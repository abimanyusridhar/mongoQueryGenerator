import re
import json
from transformers import pipeline
from utils.db_connection import get_connection

# Load pre-trained NLP model (e.g., fine-tuned T5)
nlp_model = pipeline('text2text-generation', model="t5-small")

# Define regular expression patterns for common and advanced queries
RE_PATTERNS = {
    "greater_than": re.compile(r"(find|show|get) all (\w+) where (\w+) (is|are)? greater than (\d+)"),
    "less_than": re.compile(r"(find|show|get) all (\w+) where (\w+) (is|are)? less than (\d+)"),
    "equals": re.compile(r"(find|show|get) all (\w+) where (\w+) (is|are)? (\w+)"),
    "contains": re.compile(r"(find|show|get) all (\w+) containing (\w+)"),
    "range_query": re.compile(r"(find|show|get) all (\w+) where (\w+) is between (\d+) and (\d+)"),
    "not_equals": re.compile(r"(find|show|get) all (\w+) where (\w+) (is not|isn't|are not|aren't) (\w+)"),
    "field_in_list": re.compile(r"(find|show|get) all (\w+) where (\w+) (is|are)? in \[(.*?)\]"),
    "starts_with": re.compile(r"(find|show|get) all (\w+) where (\w+) starts with (\w+)"),
    "ends_with": re.compile(r"(find|show|get) all (\w+) where (\w+) ends with (\w+)"),
}

def extract_schema():
    """
    Extracts the schema from the current MongoDB database.

    Returns:
        dict: Database schema, including collections and their fields.
    """
    client = get_connection()
    if not client:
        raise Exception("No active MongoDB connection.")

    db = client.get_default_database()
    schema = {}

    for collection in db.list_collection_names():
        sample_doc = db[collection].find_one() or {}
        schema[collection] = {"fields": list(sample_doc.keys())}

    return schema

def generate_query_from_pattern(nl_query, schema):
    """
    Generates MongoDB query using predefined regular expressions for common and advanced query patterns.

    Args:
        nl_query (str): The natural language query.
        schema (dict): The database schema.

    Returns:
        dict: MongoDB query if a pattern match is found, None otherwise.
    """
    for query_type, pattern in RE_PATTERNS.items():
        match = pattern.search(nl_query.lower())
        if match:
            collection = match.group(2)
            if collection not in schema:
                raise ValueError(f"Collection '{collection}' not found in the database.")

            field = match.group(3)
            if field not in schema[collection]['fields']:
                raise ValueError(f"Field '{field}' not found in collection '{collection}'.")

            if query_type == "greater_than":
                return {"collection": collection, "filter": {field: {"$gt": int(match.group(5))}}}
            elif query_type == "less_than":
                return {"collection": collection, "filter": {field: {"$lt": int(match.group(5))}}}
            elif query_type == "equals":
                return {"collection": collection, "filter": {field: match.group(5)}}
            elif query_type == "contains":
                return {"collection": collection, "filter": {field: {"$regex": match.group(3), "$options": "i"}}}
            elif query_type == "range_query":
                lower = int(match.group(4))
                upper = int(match.group(5))
                return {"collection": collection, "filter": {field: {"$gte": lower, "$lte": upper}}}
            elif query_type == "not_equals":
                return {"collection": collection, "filter": {field: {"$ne": match.group(5)}}}
            elif query_type == "field_in_list":
                values = [v.strip() for v in match.group(5).split(',')]
                return {"collection": collection, "filter": {field: {"$in": values}}}
            elif query_type == "starts_with":
                return {"collection": collection, "filter": {field: {"$regex": f"^{match.group(4)}", "$options": "i"}}}
            elif query_type == "ends_with":
                return {"collection": collection, "filter": {field: {"$regex": f"{match.group(4)}$", "$options": "i"}}}
    
    return None

def generate_query_with_nlp(nl_query, schema=None):
    """
    Uses an NLP model to generate a MongoDB query for more complex or ambiguous natural language queries.

    Args:
        nl_query (str): The natural language query.
        schema (dict, optional): The database schema. Default is None.

    Returns:
        dict: Generated MongoDB query.
    """
    prompt = f"Query: {nl_query}\n\nMongoDB Query:"
    if schema:
        schema_context = json.dumps(schema, indent=2)
        prompt = f"Schema:\n{schema_context}\n\n{prompt}"
    
    try:
        result = nlp_model(prompt, max_length=150, num_return_sequences=1)
        return json.loads(result[0]['generated_text'])
    except Exception as e:
        print(f"Error in NLP query generation: {e}")
        return None

def generate_mongo_query(nl_query):
    """
    Main function for generating MongoDB queries using a hybrid approach.

    Args:
        nl_query (str): The natural language query.

    Returns:
        dict: MongoDB query if successful, None otherwise.
    """
    try:
        schema = extract_schema()

        # Attempt RE-based query generation for simple queries
        query = generate_query_from_pattern(nl_query, schema)
        if query:
            print("Query generated using pattern matching.")
            return query

        # If no pattern match, fallback to NLP model for complex queries
        query = generate_query_with_nlp(nl_query, schema)
        if query:
            print("Query generated using NLP model.")
            return query

        print("Unable to generate query from input.")
        return None
    except Exception as e:
        print(f"Error in query generation: {e}")
        return None

if __name__ == "__main__":
    # Example usage
    query_input = "Find all employees where age is greater than 30"
    result = generate_mongo_query(query_input)
    if result:
        print("Generated Query:", result)
    else:
        print("Query generation failed.")
