from contextlib import contextmanager
from bson import ObjectId, DBRef
from pymongo import MongoClient, errors
from typing import Dict, Any, List, Optional
from collections import defaultdict
import json
import logging

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

mongo_client = None

@contextmanager
def get_mongo_client(host: str, port: int):
    """
    Context manager for MongoDB connection with optimized settings.

    :param host: Host address for MongoDB server.
    :param port: Port number for MongoDB server.
    :yield: MongoClient instance.
    """
    global mongo_client
    try:
        if mongo_client is None or not mongo_client.admin.command('ping')['ok']:
            mongo_client = MongoClient(
                host=host,
                port=port,
                serverSelectionTimeoutMS=5000,
                connectTimeoutMS=10000,
                socketTimeoutMS=30000,
                maxPoolSize=100,
                minPoolSize=10
            )
            mongo_client.admin.command('ping')  # Test connection
        yield mongo_client
    except errors.ServerSelectionTimeoutError as e:
        logger.error(f"Failed to connect to MongoDB: {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected MongoDB connection error: {e}")
        raise

def infer_mongodb_type(value: Any) -> str:
    """
    Infer the MongoDB type from a Python value.

    :param value: The Python value to infer type from.
    :return: The inferred MongoDB type as a string.
    """
    if isinstance(value, dict):
        if '$date' in value:
            return 'date'
        elif '$oid' in value:
            return 'ObjectId'
        else:
            return 'object'
    elif isinstance(value, list):
        if value:
            return f"array of {infer_mongodb_type(value[0])}"
        else:
            return 'array'
    elif isinstance(value, (ObjectId, DBRef)):
        return 'ObjectId'
    else:
        return type(value).__name__

def analyze_field(collection, field: str, sample_size: int = 1000) -> Dict[str, Any]:
    """
    Analyze a field in a MongoDB collection for type, distinct values, and nullability.

    :param collection: MongoDB collection object.
    :param field: Name of the field to analyze.
    :param sample_size: Number of documents to sample for analysis.
    :return: Dictionary with field analysis data.
    """
    try:
        if collection.count_documents({field: {"$exists": True}}, limit=1) == 0:
            return {
                "type": "unknown",
                "distinct_values_count": 0,
                "top_values": [],
                "nullable_count": 0,
                "non_null_count": 0
            }

        pipeline = [
            {"$match": {field: {"$exists": True}}},
            {"$sample": {"size": sample_size}},
            {"$group": {
                "_id": f"${field}",
                "count": {"$sum": 1}
            }},
            {"$sort": {"count": -1}},
            {"$limit": 5}
        ]

        distinct_values = list(collection.aggregate(pipeline))
        sample_value = collection.find_one({field: {"$exists": True}}, {field: 1}) or {}

        return {
            "type": infer_mongodb_type(sample_value.get(field)),
            "distinct_values_count": len(distinct_values),
            "top_values": [{"value": item['_id'], "count": item['count']} for item in distinct_values],
            "nullable_count": collection.count_documents({field: None}),
            "non_null_count": collection.count_documents({field: {"$exists": True, "$ne": None}})
        }
    except Exception as e:
        logger.error(f"Error analyzing field '{field}': {e}")
        return {
            "type": "unknown",
            "distinct_values_count": 0,
            "top_values": [],
            "nullable_count": 0,
            "non_null_count": 0
        }

def generate_schema(databases: Optional[List[str]] = None) -> Dict[str, Dict[Any, Any]]:
    """Generate schema from MongoDB databases and collections."""
    schema = {}
    with get_mongo_client('localhost', 27017) as client:
        db_names = databases or [db for db in client.list_database_names() 
                               if db not in ["admin", "config", "local"]]

        for db_name in db_names:
            db = client[db_name]
            db_schema = {}

            for collection_name in db.list_collection_names():
                collection = db[collection_name]
                try:
                    # Ensure we get a document with _id field
                    sample_data = next(collection.aggregate([
                        {"$project": {"_id": 1, "document": "$$ROOT"}},
                        {"$sample": {"size": 1}}
                    ]), {})
                    
                    if not sample_data:
                        continue

                    fields = ['_id']  # Always include _id field
                    field_details = {
                        '_id': {
                            'type': 'ObjectId',
                            'required': True,
                            'nullable': False
                        }
                    }

                    def _process_nested(obj, prefix=''):
                        if isinstance(obj, dict):
                            for key, value in obj.items():
                                if key == '_id':
                                    continue  # Skip _id as it's already processed
                                full_key = f"{prefix}.{key}" if prefix else key
                                fields.append(full_key)
                                if collection.count_documents({full_key: {"$exists": True}}, limit=2) > 1:
                                    field_details[full_key] = analyze_field(collection, full_key)
                                _process_nested(value, full_key)
                        elif isinstance(obj, list) and obj:
                            if isinstance(obj[0], (dict, list)):
                                _process_nested(obj[0], prefix)

                    _process_nested(sample_data.get('document', {}))

                    # Get collection statistics
                    stats = next(collection.aggregate([
                        {"$group": {
                            "_id": None,
                            "total_documents": {"$sum": 1},
                            "avg_size": {"$avg": {"$bsonSize": "$$ROOT"}}
                        }}
                    ]), {})

                    db_schema[collection_name] = {
                        "fields": fields,
                        "field_details": field_details,
                        "sample_data": [sample_data.get('document', {})],
                        "total_documents": stats.get("total_documents", 0),
                        "avg_document_size": round(stats.get("avg_size", 0) / 1024, 2),
                        "collection_size": round(
                            db.command({"collStats": collection_name}).get("size", 0) / (1024 * 1024), 
                            2
                        ),
                        "indexes": [
                            {"name": name, "fields": list(index_info["key"].items())} 
                            for name, index_info in collection.index_information().items()
                        ]
                    }

                except Exception as e:
                    logger.error(f"Error processing collection '{collection_name}': {str(e)}")
                    continue

            schema[db_name] = db_schema

    return schema

def extract_relationships(schema: Dict[str, Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """
    Extract relationships between collections based on schema details.

    :param schema: The generated schema from MongoDB.
    :return: Relationships between collections.
    """
    relationships = defaultdict(list)
    for db_name, db_schema in schema.items():
        for collection_name, details in db_schema.items():
            for field, field_info in details.get("field_details", {}).items():
                if field_info.get("type") in ["ObjectId", "DBRef"]:
                    ref_collection = field.split('.')[0] if '.' in field else collection_name
                    relationships[ref_collection].append({
                        "from_db": db_name,
                        "from_collection": collection_name,
                        "field": field,
                        "type": field_info.get("type")
                    })
    return dict(relationships)

def serialize_schema(schema: Dict[str, Dict]) -> str:
    """
    Serialize the schema into a JSON string.

    :param schema: Dictionary containing database schema information.
    :return: JSON string of the serialized schema.
    """
    def default_serializer(obj):
        if isinstance(obj, (ObjectId, DBRef)):
            return str(obj)
        raise TypeError(f"Object of type {obj.__class__.__name__} is not JSON serializable")

    return json.dumps(schema, indent=2, default=default_serializer)