import re
import json
import logging
from typing import Optional, Dict, Any
from transformers import pipeline
from utils.db_connection import get_connection

# Initialize logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# Load pre-trained NLP model
try:
    nlp_model = pipeline("text2text-generation", model="t5-large")
    logging.info("NLP model loaded successfully.")
except Exception as e:
    logging.error(f"Failed to load NLP model: {e}")
    nlp_model = None  # Instead of raising an error, set to None for later checking

# Regex patterns for natural language query recognition
RE_PATTERNS = {
    "greater_than": re.compile(r"(find|show|get) all (\w+) where (\w+) (is|are)? greater than (\d+)", re.IGNORECASE),
    "less_than": re.compile(r"(find|show|get) all (\w+) where (\w+) (is|are)? less than (\d+)", re.IGNORECASE),
    "equals": re.compile(r"(find|show|get) all (\w+) where (\w+) (is|are)? (\w+)", re.IGNORECASE),
    "contains": re.compile(r"(find|show|get) all (\w+) where (\w+) contains (\w+)", re.IGNORECASE),  # Updated regex
    "range_query": re.compile(r"(find|show|get) all (\w+) where (\w+) is between (\d+) and (\d+)", re.IGNORECASE),
    "not_equals": re.compile(r"(find|show|get) all (\w+) where (\w+) (is not|isn't|are not|aren't) (\w+)", re.IGNORECASE),
    "field_in_list": re.compile(r"(find|show|get) all (\w+) where (\w+) (is|are)? in \[(.*?)\]", re.IGNORECASE),
    "starts_with": re.compile(r"(find|show|get) all (\w+) where (\w+) starts with (\w+)", re.IGNORECASE),
    "ends_with": re.compile(r"(find|show|get) all (\w+) where (\w+) ends with (\w+)", re.IGNORECASE),
    "is_null": re.compile(r"(find|show|get) all (\w+) where (\w+) (is null)", re.IGNORECASE),
    "is_not_null": re.compile(r"(find|show|get) all (\w+) where (\w+) (is not null)", re.IGNORECASE),
}

# Cached schema
_cached_schema: Optional[Dict[str, Any]] = None

def extract_schema() -> Dict[str, Any]:
    """Extract schema from MongoDB."""
    global _cached_schema
    if _cached_schema:
        logging.info("Using cached schema.")
        return _cached_schema

    client = get_connection()
    if not client:
        raise ConnectionError("Failed to connect to MongoDB.")

    schema = {}
    try:
        db = client.get_default_database()
        for collection in db.list_collection_names():
            sample_doc = db[collection].find_one() or {}
            schema[collection] = {
                "fields": list(sample_doc.keys()),
                "field_types": {key: type(value).__name__ for key, value in sample_doc.items()},
            }
        _cached_schema = schema
        logging.info("Schema successfully extracted and cached.")
        return schema
    except Exception as e:
        logging.error(f"Error extracting schema: {e}")
        raise RuntimeError("Error while extracting schema.") from e

def build_filter(query_type: str, field: str, value: str) -> Dict[str, Any]:
    """Construct MongoDB filter."""
    filter_map = {
        "greater_than": lambda v: {field: {"$gt": int(v)}},
        "less_than": lambda v: {field: {"$lt": int(v)}},
        "equals": lambda v: {field: v},
        "contains": lambda v: {field: {"$regex": v, "$options": "i"}},
        "range_query": lambda v: {
            field: {"$gte": int(v.split(",")[0]), "$lte": int(v.split(",")[1])}
        },
        "not_equals": lambda v: {field: {"$ne": v}},
        "field_in_list": lambda v: {field: {"$in": [i.strip() for i in v.split(",") if i.strip()]}},
        "starts_with": lambda v: {field: {"$regex": f"^{v}", "$options": "i"}},
        "ends_with": lambda v: {field: {"$regex": f"{v}$", "$options": "i"}},
        "is_null": lambda _: {field: None},
        "is_not_null": lambda _: {field: {"$ne": None}},
    }
    
    if query_type not in filter_map:
        raise ValueError(f"Unsupported query type: {query_type}")
    return filter_map[query_type](value)

def generate_query_from_pattern(nl_query: str, schema: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Generate query using regex patterns."""
    for query_type, pattern in RE_PATTERNS.items():
        match = pattern.search(nl_query)
        if match:
            collection = match.group(2)
            field = match.group(3)
            
            if collection not in schema:
                raise ValueError(f"Collection '{collection}' not found in schema.")
            if field not in schema[collection]["fields"]:
                raise ValueError(f"Field '{field}' not found in collection '{collection}'.")
            
            # Correct group index for different query types
            value_index = 5 if query_type not in ["field_in_list", "range_query"] else 6 if query_type == "field_in_list" else 4
            value = match.group(value_index) or ""
            
            return {"collection": collection, "filter": build_filter(query_type, field, value)}
    return None

def generate_query_with_nlp(nl_query: str, schema: Dict[str, Any]) -> Dict[str, Any]:
    """Generate query using the NLP model."""
    if not nlp_model:
        raise RuntimeError("NLP model not available.")

    try:
        schema_json = json.dumps(schema, indent=2)
        prompt = f"Schema:\n{schema_json}\n\nQuery: {nl_query}\n\nMongoDB Query:"
        result = nlp_model(prompt, max_length=200, num_return_sequences=1)
        generated_query = result[0]["generated_text"].strip()
        
        try:
            # Attempt to parse the generated query as JSON
            return json.loads(generated_query)
        except json.JSONDecodeError:
            logging.warning(f"Generated query is not valid JSON: {generated_query}")
            return {"collection": "unknown", "filter": {}, "error": "Invalid JSON from NLP model"}
    except Exception as e:
        logging.error(f"NLP query generation failed: {e}")
        raise RuntimeError("NLP query generation failed.") from e

def generate_mongo_query(nl_query: str) -> Optional[Dict[str, Any]]:
    """Generate MongoDB query using regex and NLP fallback."""
    try:
        schema = extract_schema()
        # First try with regex patterns
        query = generate_query_from_pattern(nl_query, schema)
        if query:
            logging.info("Query generated using regex patterns.")
            return query
        
        # Fallback to NLP if regex fails
        if nlp_model:
            logging.info("Falling back to NLP model for query generation.")
            return generate_query_with_nlp(nl_query, schema)
        else:
            logging.warning("NLP model not available; unable to generate query.")
            return None
    except Exception as e:
        logging.error(f"Error generating query: {e}")
        return None

if __name__ == "__main__":
    query_input = "Find all employees where age is greater than 30"
    result = generate_mongo_query(query_input)
    if result:
        logging.info(f"Generated Query: {json.dumps(result, indent=2)}")
    else:
        logging.error("Query generation failed.")