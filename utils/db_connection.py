import csv
import json
import logging
import os
import re
from contextlib import contextmanager
from typing import Any, Dict, Iterator, Generator, List, Tuple
from pymongo import InsertOne, MongoClient, errors
from dotenv import load_dotenv
from tqdm import tqdm
import ijson
from tenacity import retry, stop_after_attempt, wait_exponential
import bson
from bson import ObjectId

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Precompiled regex patterns
_COLLECTION_NAME_SANITIZE_PATTERN = re.compile(r'[^\w-]')
_INT_PATTERN = re.compile(r'^[-+]?\d+$')
_FLOAT_PATTERN = re.compile(r'^[-+]?(\d+\.\d*|\.\d+)([eE][-+]?\d+)?$')
mongo_client = None


@contextmanager
def get_mongo_client_with_context(db_details: Dict[str, Any]) -> Iterator[MongoClient]:
    client = None
    try:
        client = MongoClient(
            host=db_details['host'],
            port=db_details['port'],
            serverSelectionTimeoutMS=5000,
            connectTimeoutMS=10000,
            socketTimeoutMS=30000,
            maxPoolSize=100,
            minPoolSize=10
        )
        # Check connection by pinging the server
        client.admin.command('ping')  # Removed try-except here as it's redundant with the broader try-except
        yield client
    except errors.ServerSelectionTimeoutError as e:
        logger.error(f"MongoDB connection timeout: {e}")
        raise
    except errors.ConnectionFailure as e:
        logger.error(f"MongoDB connection failed: {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error while connecting to MongoDB: {e}")
        raise
    finally:
        if client:
            try:
                client.close()
            except Exception as e:
                logger.warning(f"Failed to close MongoDB connection: {e}")

def sanitize_collection_name(name: str) -> str:
    return _COLLECTION_NAME_SANITIZE_PATTERN.sub('_', name).strip('_')[:63]

def convert_csv_types(row: Dict[str, str]) -> Dict[str, Any]:
    converted = {}
    for key, value in row.items():
        value = value.strip()
        if not value:
            converted[key] = None
            continue

        if value[0] in ('{', '[') and value[-1] in ('}', ']'):
            try:
                converted[key] = json.loads(value)
            except json.JSONDecodeError:
                logger.warning(f"Failed to decode JSON for key {key}: {value}")
                converted[key] = value
            continue

        lower_val = value.lower()
        if lower_val in ('true', 'false'):
            converted[key] = lower_val == 'true'
            continue

        if _INT_PATTERN.match(value):
            converted[key] = int(value)
        elif _FLOAT_PATTERN.match(value):
            converted[key] = float(value)
        else:
            converted[key] = value
    return converted

def process_file(file_path: str, db_name: str, db_details: Dict[str, Any]) -> Tuple[bool, str]:
    ext = os.path.splitext(file_path)[1].lower()
    if ext == '.csv':
        return _process_csv_streaming(file_path, db_name, db_details)
    elif ext == '.json':
        return _process_json_streaming(file_path, db_name, db_details)
    else:
        return False, f"Unsupported file type: {ext}"

def _process_csv_streaming(csv_path: str, db_name: str, db_details: Dict[str, Any]) -> Tuple[bool, str]:
    try:
        if not os.path.exists(csv_path):
            return False, "File not found"

        with open(csv_path, 'r', encoding='utf-8') as f:
            total_rows = sum(1 for _ in csv.reader(f)) - 1

        collection_name = sanitize_collection_name(os.path.splitext(os.path.basename(csv_path))[0])

        with open(csv_path, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            return _process_generator(
                data_generator=(convert_csv_types(row) for row in reader),
                db_name=db_name,
                collection_name=collection_name,
                db_details=db_details,
                total=total_rows
            )

    except Exception as e:
        logger.error(f"CSV processing failed: {str(e)}", exc_info=True)
        return False, f"CSV error: {str(e)}"

def _process_json_streaming(json_path: str, db_name: str, db_details: Dict[str, Any]) -> Tuple[bool, str]:
    try:
        if not os.path.exists(json_path):
            return False, "File not found"

        with open(json_path, 'r', encoding='utf-8') as f:
            first_char = f.read(1)
            f.seek(0)
            
            if first_char == '[':
                # Handle array of documents
                documents = ijson.items(f, 'item')
                return _process_generator(
                    data_generator=(ensure_document_id(doc) for doc in documents),
                    db_name=db_name,
                    collection_name=sanitize_collection_name(os.path.splitext(os.path.basename(json_path))[0]),
                    db_details=db_details
                )
            elif first_char == '{':
                # Handle collection-based structure
                collections = ijson.kvitems(f, '')
                success = True
                messages = []
                
                for collection_name, items in collections:
                    valid_name = sanitize_collection_name(collection_name)
                    # Ensure each document has _id
                    processed_items = (ensure_document_id(item) for item in items)
                    s, msg = _process_generator(
                        data_generator=processed_items,
                        db_name=db_name,
                        collection_name=valid_name,
                        db_details=db_details
                    )
                    messages.append(f"{valid_name}: {msg}")
                    success &= s
                return success, " | ".join(messages)
            else:
                return False, "Unsupported JSON structure"

    except Exception as e:
        logger.error(f"JSON processing failed: {str(e)}", exc_info=True)
        return False, f"JSON error: {str(e)}"

def _process_generator(data_generator: Generator[Dict, None, None], db_name: str,
                      collection_name: str, db_details: Dict[str, Any],
                      total: int = None) -> Tuple[bool, str]:
    try:
        with get_mongo_client_with_context(db_details) as client:
            db = client[db_name]
            collection = db[collection_name]

            batch = []
            total_inserted = 0
            batch_size = 5000
            max_batch_size = 15000
            batch_size_bytes = 0
            max_batch_bytes = 12 * 1024 * 1024

            with tqdm(total=total, desc=f"Inserting to {collection_name}") as pbar:
                for doc in data_generator:
                    doc_size = len(bson.BSON.encode(doc))

                    if batch_size_bytes + doc_size > max_batch_bytes or len(batch) >= batch_size:
                        inserted = _insert_batch_with_retry(collection, batch)
                        total_inserted += inserted
                        pbar.update(len(batch))

                        if inserted == len(batch):
                            batch_size = min(batch_size * 2, max_batch_size)
                        else:
                            batch_size = max(batch_size // 2, 1000)

                        batch = []
                        batch_size_bytes = 0

                    batch.append(doc)
                    batch_size_bytes += doc_size

                if batch:
                    inserted = _insert_batch_with_retry(collection, batch)
                    total_inserted += inserted
                    pbar.update(len(batch))

            logger.info(f"Inserted {total_inserted} documents into {collection_name}")
            return True, f"Inserted {total_inserted} documents"

    except errors.ServerSelectionTimeoutError as e:
        logger.error(f"Could not connect to server for collection {collection_name}: {e}")
        return False, f"Server selection timeout error: {str(e)}"
    except errors.OperationFailure as e:
        logger.error(f"Operation failed in collection {collection_name}: {e}")
        return False, f"MongoDB operation error: {str(e)}"
    except Exception as e:
        logger.error(f"Processing failed for collection {collection_name}: {str(e)}", exc_info=True)
        return False, f"General processing error: {str(e)}"

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=10))
def _insert_batch_with_retry(collection, batch: List[Dict]) -> int:
    """Insert batch of documents with retry logic."""
    try:
        # Ensure each document in batch has _id
        processed_batch = [ensure_document_id(doc) for doc in batch]
        result = collection.bulk_write(
            [InsertOne(doc) for doc in processed_batch],
            ordered=False,
            bypass_document_validation=True
        )
        return result.inserted_count
    except errors.BulkWriteError as e:
        logger.warning(f"Bulk write error in collection {collection.name}: {e.details}")
        return e.details['nInserted']
    except Exception as e:
        logger.error(f"Unexpected error during batch insert: {e}")
        raise

def get_connection_details() -> Dict[str, Any]:
    load_dotenv()
    details = {
        'host': os.getenv('MONGO_HOST', 'localhost'),
        'port': int(os.getenv('MONGO_PORT', '27017'))
    }
    if not all(details.values()):
        raise ValueError("Required environment variables MONGO_HOST or MONGO_PORT are missing.")
    return details

def ensure_document_id(document: Dict[str, Any]) -> Dict[str, Any]:
    """Ensure document has _id field."""
    if not isinstance(document, dict):
        raise ValueError("Document must be a dictionary")
    if '_id' not in document:
        document['_id'] = ObjectId()
    return document