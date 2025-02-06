import re
import json
import logging
import datetime
from typing import List, Optional, Dict, Any
from transformers import pipeline
from flask import Flask, request, jsonify
from bson import ObjectId, DBRef
from pymongo import MongoClient

app = Flask(__name__)

# Configure logging with less verbosity for production
logging.basicConfig(level=logging.WARNING, format="%(asctime)s - %(levelname)s - %(message)s")

# Load pre-trained NLP model once
nlp_model = None
try:
    nlp_model = pipeline("text2text-generation", model="t5-large")
    logging.info("NLP model loaded successfully.")
except Exception as e:
    logging.error(f"Failed to load NLP model: {e}")

# Regex patterns for natural language query recognition
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

# Cached schema
_cached_schema: Optional[Dict[str, Any]] = None
_schema_cache_time: float = 0

# Persistent MongoDB connection
mongo_client = None

def get_connection():
    global mongo_client
    if mongo_client is None:
        try:
            mongo_client = MongoClient('localhost', 27017)  # Use your actual MongoDB connection settings
            mongo_client.admin.command('ping')  # Test connection
            logging.info("MongoDB connection established.")
        except Exception as e:
            logging.error(f"Failed to connect to MongoDB: {e}")
            raise
    return mongo_client

def close_connection():
    global mongo_client
    if mongo_client:
        try:
            mongo_client.close()
            logging.info("MongoDB connection closed.")
        except Exception as e:
            logging.error(f"Error closing MongoDB connection: {e}")

# Text preprocessing utilities
def preprocess_text(text: str) -> str:
    """
    Preprocess natural language input for better parsing.
    - Normalize whitespace
    - Remove special characters (except those needed for queries)
    - Convert to lowercase
    - Trim leading/trailing spaces
    """
    if not text:
        return text

    # Normalize whitespace and trim
    text = re.sub(r"\s+", " ", text).strip()

    # Remove unwanted special characters (keep quotes, commas, and basic punctuation)
    text = re.sub(r"[^\w\s,'\"\.\-]", "", text)

    # Convert to lowercase for case-insensitive matching
    text = text.lower()

    return text

def extract_schema() -> Dict[str, Any]:
    """Extract schema from MongoDB with cache invalidation after 60 seconds."""
    global _cached_schema, _schema_cache_time
    current_time = datetime.datetime.now().timestamp()

    if _cached_schema and (current_time - _schema_cache_time < 60):
        return _cached_schema

    client = get_connection()
    try:
        db = client.get_default_database()
        schema = {}
        for collection_name in db.list_collection_names():
            doc = db[collection_name].find_one()
            if doc:
                fields = list(doc.keys())
                field_types = {k: infer_mongodb_type(v) for k, v in doc.items()}
            else:
                fields = []
                field_types = {}
            schema[collection_name] = {"fields": fields, "field_types": field_types}

        _cached_schema = schema
        _schema_cache_time = current_time
        logging.info("Schema successfully extracted and cached.")
        return schema
    except Exception as e:
        logging.error(f"Error extracting schema: {e}")
        raise RuntimeError("Error while extracting schema.") from e

def infer_mongodb_type(value: Any) -> str:
    """Infer MongoDB type from Python value."""
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

def convert_value(value: str, target_type: str) -> Any:
    """Convert string value to appropriate type based on schema."""
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
    """Construct MongoDB filter with type conversion."""
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

def generate_query_from_pattern(nl_query: str, schema: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Generate query using regex patterns with enhanced value handling."""
    for query_type, pattern in RE_PATTERNS.items():
        match = pattern.search(nl_query)
        if not match:
            continue

        groups = match.groups()
        collection = groups[0]
        field = groups[1]

        if collection not in schema:
            logging.warning(f"Collection '{collection}' not found in schema.")
            return None
        if field not in schema[collection]["fields"]:
            logging.warning(f"Field '{field}' not found in collection '{collection}'.")
            return None

        field_type = schema[collection]["field_types"].get(field, "string")
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
                "collection": collection,
                "filter": build_filter(query_type, field, value, field_type)
            }
        except Exception as e:
            logging.error(f"Error building filter: {e}")
            return None
    return None

def generate_query_with_nlp(nl_query: str, schema: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Generate query using NLP model."""
    if not nlp_model:
        return None

    try:
        schema_json = json.dumps(schema, default=str)
        prompt = f"""Convert this natural language query to MongoDB JSON format:
        Schema: {schema_json}
        Query: {nl_query}
        Result: {{"collection": "...", "filter": {{...}}}}"""

        result = nlp_model(prompt, max_length=512, num_return_sequences=1)
        generated_text = result[0]["generated_text"].strip()

        # Attempt to extract JSON from the generated text
        json_match = re.search(r"\{.*\}", generated_text, re.DOTALL)
        if json_match:
            return json.loads(json_match.group())
        return None
    except Exception as e:
        logging.error(f"NLP query generation failed: {e}")
        return None

def generate_mongo_query(nl_query: str) -> Optional[Dict[str, Any]]:
    """Generate MongoDB query using regex and NLP fallback."""
    try:
        schema = extract_schema()
        regex_query = generate_query_from_pattern(nl_query, schema)
        if regex_query:
            return regex_query
        return generate_query_with_nlp(nl_query, schema)
    except Exception as e:
        logging.error(f"Query generation error: {e}")
        return None

def execute_query(mongo_query: Dict[str, Any]) -> Optional[List[Dict[str, Any]]]:
    """Execute MongoDB query with safety checks and projection for efficiency."""
    client = get_connection()
    try:
        db = client.get_default_database()
        collection_name = mongo_query["collection"]

        if collection_name not in db.list_collection_names():
            logging.error(f"Collection {collection_name} does not exist.")
            return None

        # Use projection to limit returned fields to only those needed
        projection = {field: 1 for field in mongo_query.get("filter", {}).keys()}
        projection['_id'] = 1  # Always include _id

        results = db[collection_name].find(
            mongo_query.get("filter", {}),
            projection=projection,
            limit=100
        )
        return [convert_from_mongo_types(doc) for doc in results]
    except Exception as e:
        logging.error(f"Query execution error: {e}")
        return None

def convert_from_mongo_types(data: Any) -> Any:
    """Convert MongoDB types to JSON serializable types."""
    if isinstance(data, dict):
        return {k: convert_from_mongo_types(v) for k, v in data.items()}
    elif isinstance(data, list):
        return [convert_from_mongo_types(item) for item in data]
    elif isinstance(data, (ObjectId, DBRef)):
        return str(data)
    elif isinstance(data, datetime.datetime):
        return data.isoformat()
    return data

@app.route('/query', methods=['POST'])
def handle_nl_query():
    """Endpoint for natural language query processing."""
    data = request.get_json()
    if not data or 'query' not in data:
        return jsonify({"error": "Missing 'query' parameter"}), 400

    nl_query = preprocess_text(data['query'])
    if not nl_query:
        return jsonify({"error": "Empty query"}), 400

    mongo_query = generate_mongo_query(nl_query)
    if not mongo_query:
        return jsonify({"error": "Could not parse query"}), 400

    results = execute_query(mongo_query)
    if results is None:
        return jsonify({"error": "Query execution failed"}), 500

    return jsonify({"results": results})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)