import re
import json
import logging
import datetime
import os
import requests
from typing import List, Optional, Dict, Any
from bson import ObjectId, DBRef
from pymongo import MongoClient

# Configure logging
logging.basicConfig(level=logging.WARNING, format="%(asctime)s - %(levelname)s - %(message)s")

# Mistral API configuration
MISTRAL_API_URL = "https://api.mistral.ai/v1/chat/completions"
MISTRAL_API_KEY = os.environ.get("MISTRAL_API_KEY")
MODEL_NAME = "mistral-medium"

# Regular expression patterns for query parsing
RE_PATTERNS = {
    "greater_than": re.compile(r"(\w+) where (\w+) (?:is|are)? greater than (['\"]?)(.+?)\2", re.IGNORECASE),
    "less_than": re.compile(r"(\w+) where (\w+) (?:is|are)? less than (['\"]?)(.+?)\2", re.IGNORECASE),
    "equals": re.compile(r"(\w+) where (\w+) (?:is|are)? (['\"]?)(.*?)\2", re.IGNORECASE),
    "contains": re.compile(r"(\w+) where (\w+) contains (['\"]?)(.*?)\2", re.IGNORECASE),
    "range_query": re.compile(r"(\w+) where (\w+) is between (['\"]?)(.+?)\2 and (['\"]?)(.+?)\3", re.IGNORECASE),
    "not_equals": re.compile(r"(\w+) where (\w+) (?:is not|isn't|are not|aren't) (['\"]?)(.*?)\2", re.IGNORECASE),
    "field_in_list": re.compile(r"(\w+) where (\w+) (?:is|are)? in (?:\[(.*?)\]|((?:[\w\s,]++)+))", re.IGNORECASE),
    "starts_with": re.compile(r"(\w+) where (\w+) starts with (['\"]?)(.+?)\2", re.IGNORECASE),
    "ends_with": re.compile(r"(\w+) where (\w+) ends with (['\"]?)(.+?)\2", re.IGNORECASE),
    "is_null": re.compile(r"(\w+) where (\w+) (is null)", re.IGNORECASE),
    "is_not_null": re.compile(r"(\w+) where (\w+) (is not null)", re.IGNORECASE),
}

# Global variables
_cached_schema: Optional[Dict[str, Any]] = None
_schema_cache_time: float = 0
_mongo_client = None

class MongoQueryGenerator:
    def __init__(self, host: str, port: int, database: str):
        self.host = host
        self.port = port
        self.database = database
        self._client = None

    def connect(self) -> None:
        """Establish MongoDB connection"""
        global _mongo_client
        if not _mongo_client:
            try:
                connection_string = f"mongodb://{self.host}:{self.port}"
                _mongo_client = MongoClient(connection_string)
                _mongo_client.admin.command('ping')
                logging.info("MongoDB connection established.")
            except Exception as e:
                logging.error(f"Failed to connect to MongoDB: {e}")
                raise
        self._client = _mongo_client

    def close(self) -> None:
        """Close MongoDB connection"""
        global _mongo_client
        if _mongo_client:
            try:
                _mongo_client.close()
                _mongo_client = None
                self._client = None
                logging.info("MongoDB connection closed.")
            except Exception as e:
                logging.error(f"Error closing MongoDB connection: {e}")

    def generate_query(self, nl_query: str) -> Optional[Dict[str, Any]]:
        """Generate MongoDB query from natural language input"""
        try:
            schema = self._extract_schema()
            regex_query = self._generate_query_from_pattern(nl_query, schema)
            return regex_query if regex_query else self._generate_query_with_nlp(nl_query, schema)
        except Exception as e:
            logging.error(f"Query generation error: {e}")
            return None

    def execute_query(self, mongo_query: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Execute generated MongoDB query"""
        try:
            if not self._client:
                self.connect()
            
            db = self._client[self.database]
            collection_name = mongo_query["collection"]

            if collection_name not in db.list_collection_names():
                logging.error(f"Collection {collection_name} does not exist.")
                return None

            filter_dict = mongo_query.get("filter", {})
            fields = self._extract_fields(filter_dict)
            projection = {field: 1 for field in fields}
            projection['_id'] = 1

            total_count = db[collection_name].count_documents(filter_dict)
            results = db[collection_name].find(filter_dict, projection=projection, limit=100)
            return {
                "total": total_count,
                "results": [self._convert_from_mongo_types(doc) for doc in results]
            }
        except Exception as e:
            logging.error(f"Query execution error: {e}")
            return None

    # ... Keep all the helper methods (make them private with _) ...
    def _extract_schema(self) -> Dict[str, Any]:
        global _cached_schema, _schema_cache_time
        current_time = datetime.datetime.now().timestamp()

        if _cached_schema and (current_time - _schema_cache_time < 60):
            return _cached_schema

        client = self._client
        try:
            selected_db = requests.session.get('selected_db')
            if not selected_db:
                raise ValueError("No database selected in session.")
            db = client[selected_db['database']]
            schema = {}
            for collection_name in db.list_collection_names():
                lower_collection = collection_name.lower()
                doc = db[collection_name].find_one()
                field_info = {}
                if doc:
                    for field, value in doc.items():
                        lower_field = field.lower()
                        field_info[lower_field] = {
                            'original_name': field,
                            'type': infer_mongodb_type(value)
                        }
                schema[lower_collection] = {
                    'original_name': collection_name,
                    'fields': field_info
                }
            _cached_schema = schema
            _schema_cache_time = current_time
            return schema
        except Exception as e:
            logging.error(f"Error extracting schema: {e}")
            raise

    def _generate_query_from_pattern(self, nl_query: str, schema: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        for query_type, pattern in RE_PATTERNS.items():
            match = pattern.search(nl_query)
            if not match:
                continue

            groups = match.groups()
            collection = groups[0].lower()
            field = groups[1].lower()

            if collection not in schema:
                logging.warning(f"Collection '{collection}' not found in schema.")
                return None
            collection_info = schema[collection]
            original_collection = collection_info['original_name']
            field_info = collection_info['fields'].get(field)
            if not field_info:
                logging.warning(f"Field '{field}' not found in collection '{collection}'.")
                return None
            original_field = field_info['original_name']
            field_type = field_info['type']

            value = None
            if query_type == "range_query":
                start = convert_value(groups[3].strip("'\""), field_type)
                end = convert_value(groups[5].strip("'\""), field_type)
                value = (start, end)
            elif query_type in ["field_in_list"]:
                value = groups[2] or groups[3]
            elif query_type in ["is_null", "is_not_null"]:
                value = None
            else:
                quote_char = groups[2] if len(groups) > 2 else None
                value = groups[3] if len(groups) > 3 else groups[2]
                if quote_char and value:
                    value = value.strip(quote_char)

            try:
                return {
                    "collection": original_collection,
                    "filter": build_filter(query_type, original_field, value, field_type)
                }
            except Exception as e:
                logging.error(f"Error building filter: {e}")
                return None
        return None

    def _generate_query_with_nlp(self, nl_query: str, schema: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        try:
            schema_json = json.dumps(schema, default=str)
            prompt = f"""Generate a MongoDB query as valid JSON with 'collection' and 'filter' keys based on this schema.

            Database Schema:
            {schema_json}

            Natural Language Query:
            {nl_query}

            Output the JSON query only, without any additional text or explanation:"""

            generated_text = generate_text_with_mistral(prompt)
            cleaned_text = clean_json_output(generated_text)
            if not cleaned_text:
                return None

            result = json.loads(cleaned_text)
            if "collection" not in result or "filter" not in result:
                return None

            collection = result['collection'].lower()
            if collection not in schema:
                return None
            result['collection'] = schema[collection]['original_name']
            return result
        except json.JSONDecodeError:
            logging.error("Failed to decode generated JSON")
            return None
        except Exception as e:
            logging.error(f"NLP query generation failed: {e}")
            return None

    @staticmethod
    def _extract_fields(filter_dict: Dict) -> List[str]:
        fields = []
        for key, value in filter_dict.items():
            if key.startswith("$"):
                if isinstance(value, list):
                    for item in value:
                        if isinstance(item, dict):
                            fields.extend(MongoQueryGenerator._extract_fields(item))
            else:
                fields.append(key)
                if isinstance(value, dict):
                    fields.extend(MongoQueryGenerator._extract_fields(value))
        return list(set(fields))

    @staticmethod
    def _convert_from_mongo_types(data: Any) -> Any:
        if isinstance(data, dict):
            return {k: MongoQueryGenerator._convert_from_mongo_types(v) for k, v in data.items()}
        elif isinstance(data, list):
            return [MongoQueryGenerator._convert_from_mongo_types(item) for item in data]
        elif isinstance(data, (ObjectId, DBRef)):
            return str(data)
        elif isinstance(data, datetime.datetime):
            return data.isoformat()
        return data

# Helper functions that can be used independently
def preprocess_text(text: str) -> str:
    """Clean and normalize input text"""
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"[^\w\s,'\"\.\-]", "", text)
    return text

def convert_value(value: str, target_type: str) -> Any:
    """Convert string values to appropriate MongoDB types"""
    try:
        if target_type == "int":
            return int(value)
        elif target_type == "double":
            return float(value)
        elif target_type == "date":
            return datetime.datetime.fromisoformat(value)
        elif target_type == "boolean":
            return value.lower() in ["true", "yes", "1"]
        elif target_type == "ObjectId":
            return ObjectId(value) if ObjectId.is_valid(value) else value
        return value
    except Exception as e:
        logging.error(f"Value conversion error: {e}")
        return value

def build_filter(query_type: str, field: str, value: Any, field_type: str) -> Dict[str, Any]:
    """Build MongoDB filter based on query type"""
    converted_value = convert_value(value, field_type)
    if query_type == "greater_than":
        return {field: {"$gt": converted_value}}
    elif query_type == "less_than":
        return {field: {"$lt": converted_value}}
    elif query_type == "equals":
        return {field: converted_value}
    elif query_type == "contains":
        return {field: {"$regex": re.escape(str(converted_value)), "$options": "i"}}
    elif query_type == "range_query":
        return {field: {"$gte": converted_value[0], "$lte": converted_value[1]}}
    elif query_type == "not_equals":
        return {field: {"$ne": converted_value}}
    elif query_type == "field_in_list":
        values = [convert_value(v.strip(), field_type) for v in value.split(",")]
        return {field: {"$in": values}}
    elif query_type == "starts_with":
        return {field: {"$regex": f"^{re.escape(str(converted_value))}", "$options": "i"}}
    elif query_type == "ends_with":
        return {field: {"$regex": f"{re.escape(str(converted_value))}$", "$options": "i"}}
    elif query_type == "is_null":
        return {field: None}
    elif query_type == "is_not_null":
        return {field: {"$ne": None}}
    else:
        raise ValueError(f"Unsupported query type: {query_type}")

def infer_mongodb_type(value: Any) -> str:
    if isinstance(value, ObjectId):
        return "ObjectId"
    elif isinstance(value, datetime.datetime):
        return "date"
    elif isinstance(value, bool):
        return "boolean"
    elif isinstance(value, int):
        return "int"
    elif isinstance(value, float):
        return "double"
    elif isinstance(value, str):
        return "string"
    elif isinstance(value, list):
        return "array"
    elif isinstance(value, dict):
        return "object"
    else:
        return "unknown"

def clean_json_output(text: str) -> str:
    start = text.find('{')
    if start == -1:
        return ""
    end = text.rfind('}') + 1
    return text[start:end]

def generate_text_with_mistral(prompt: str) -> str:
    try:
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {MISTRAL_API_KEY}"
        }

        messages = [{
            "role": "user",
            "content": prompt
        }]

        payload = {
            "model": MODEL_NAME,
            "messages": messages,
            "temperature": 0.3,
            "max_tokens": 512
        }

        response = requests.post(MISTRAL_API_URL, headers=headers, json=payload, timeout=30)
        response.raise_for_status()

        result = response.json()
        return result['choices'][0]['message']['content']
    except requests.exceptions.RequestException as e:
        logging.error(f"Mistral API request failed: {e}")
    except (KeyError, IndexError) as e:
        logging.error(f"Failed to parse Mistral response: {e}")
    except Exception as e:
        logging.error(f"Error generating text with Mistral: {e}")
    return ""