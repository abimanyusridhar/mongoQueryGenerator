from pymongo import MongoClient
from collections import defaultdict
from pyvis.network import Network
import logging
from datetime import datetime
from bson import ObjectId, DBRef, json_util
import json
from argparse import ArgumentParser
from typing import Dict, Any, List, Optional, Tuple

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Global MongoDB client
client: Optional[MongoClient] = None

# MongoDB connection defaults
DEFAULT_MONGODB_HOST = "localhost"
DEFAULT_MONGODB_PORT = 27017

def connect_to_db(host: str, port: int) -> MongoClient:
    """Connect to MongoDB server."""
    global client
    try:
        client = MongoClient(host=host, port=port, serverSelectionTimeoutMS=5000)
        client.admin.command("ping")  # Test the connection
        logger.info(f"Connected to MongoDB server at {host}:{port}")
        return client
    except Exception as e:
        logger.error(f"Error connecting to the database: {e}")
        if client:
            client.close()
            client = None
        raise

def infer_mongodb_type(value: Any) -> str:
    """Infer the MongoDB type from a Python value with more precision for complex types."""
    if isinstance(value, dict):
        if '$date' in value:
            return 'date'
        elif '_id' in value and '$oid' in value:
            return 'ObjectId'
        else:
            return 'object'
    elif isinstance(value, list):
        return 'array'
    elif isinstance(value, ObjectId):
        return 'ObjectId'
    elif isinstance(value, DBRef):
        return 'DBRef'
    else:
        return type(value).__name__

def generate_json_schema(data: Any) -> Dict[str, Any]:
    """Generate a JSON schema from the given data structure recursively."""
    schema = {}
    if isinstance(data, dict):
        for key, value in data.items():
            schema[key] = {"type": infer_mongodb_type(value)}
            if isinstance(value, (dict, list)) and not isinstance(value, (ObjectId, DBRef)):
                schema[key]["properties"] = generate_json_schema(value)
    elif isinstance(data, list):
        if data:
            item_schema = generate_json_schema(data[0])
            schema = {"type": "array", "items": item_schema}
        else:
            schema = {"type": "array", "items": {"type": "undefined"}}
    else:
        schema = {"type": infer_mongodb_type(data)}
    return schema

def generate_schema_from_json(json_data: Dict[str, List[Dict]]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Generate schema from JSON data."""
    schema = {}
    for collection, documents in json_data.items():
        if not isinstance(documents, list):
            documents = [documents]  # Handle case where there's only one document

        sample_data = documents[0] if documents else {}
        total_documents = len(documents)
        
        field_details = {}
        for field, value in sample_data.items():
            field_type = infer_mongodb_type(value)
            field_details[field] = {
                "type": field_type,
                "distinct_values_count": 1,  # Placeholder; should ideally count distinct values
                "nullable_count": sum(1 for doc in documents if field not in doc)
            }

        schema[collection] = {
            "fields": list(sample_data.keys()),
            "field_details": field_details,
            "total_documents": total_documents,
            "avg_document_size": 0,  # Can't calculate from JSON without actual size info
            "collection_size": 0,
            "indexes": []  # JSON files typically don't include index information
        }
    return schema, {"name": "JSON File", "collections_count": len(json_data), "size_on_disk": 0}

def generate_schema_for_db(db: Any) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Generate schema for a single database."""
    schema = {}
    db_details = {
        "name": db.name,
        "collections_count": len(db.list_collection_names()),
        "size_on_disk": db.command("dbstats").get("dataSize", 0)
    }

    for collection_name in db.list_collection_names():
        collection = db[collection_name]
        sample_data = collection.find_one() or {}

        total_documents = collection.count_documents({})
        avg_document_size = next(collection.aggregate([
            {"$limit": 1000},  # Limit aggregation for performance
            {"$group": {"_id": None, "avgSize": {"$avg": {"$bsonSize": "$$ROOT"}}}}
        ]), {}).get("avgSize", 0) / 1024  # Convert to KB

        field_details = {}
        for field, value in sample_data.items():
            field_type = infer_mongodb_type(value)
                        # Limit distinct values count for performance
            distinct_values = len(collection.distinct(field, limit=1000))
            nullable_count = collection.count_documents({field: None})
            field_details[field] = {
                "type": field_type,
                "distinct_values_count": distinct_values,
                "nullable_count": nullable_count
            }

        indexes = collection.index_information()
        index_info = [
            {"name": index_name, "fields": list(info["key"].keys())}
            for index_name, info in indexes.items()
        ]

        schema[collection_name] = {
            "fields": list(sample_data.keys()),
            "field_details": field_details,
            "total_documents": total_documents,
            "avg_document_size": avg_document_size,
            "collection_size": db.command({"collStats": collection_name}).get("size", 0),
            "indexes": index_info,
        }

    return schema, db_details

def extract_relationships(schema: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    """Extract relationships between collections based on schema details."""
    relationships = defaultdict(list)
    for collection_name, details in schema.items():
        for field, field_info in details.get("field_details", {}).items():
            if field_info.get("type") in ["DBRef", "ObjectId"]:
                ref_collection = field.split('_')[0] if field.endswith('_id') else "Unknown"
                relationships[ref_collection].append({
                    "from_collection": collection_name,
                    "field": field,
                    "referenced_id": field_info.get("id", "Unknown")
                })
    return dict(relationships)

def visualize_schema(schema: Dict[str, Any], db_details: Dict[str, Any], output_file: str = None) -> None:
    """Visualize the schema using PyVis."""
    if not output_file:
        output_file = f"schema_visualization_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"

    net = Network(height="750px", width="100%", directed=True, bgcolor="#ffffff", font_color="black")
    try:
        db_info_node = f"{db_details['name']} - Info"
        db_info_label = (
            f"{db_details['name']}\nCollections: {db_details['collections_count']}\n"
            f"Size on Disk: {db_details['size_on_disk']} bytes"
        )
        net.add_node(db_info_node, label=db_info_label, shape="box", color="#2ecc71")

        for collection, details in schema.items():
            collection_label = (
                f"{collection}\nDocuments: {details['total_documents']}\nAvg Size:
                                f"{details['avg_document_size']:.2f} KB\nSize: {details['collection_size']} bytes"
            )
            net.add_node(collection, label=collection_label, shape="ellipse", color="#76c7c0")
            net.add_edge(db_info_node, collection, label="Contains", color="#2ecc71")

            for field, field_info in details.get("field_details", {}).items():
                field_node = f"{collection}.{field}"
                net.add_node(
                    field_node,
                    label=(
                        f"{field} ({field_info['type']})\nDistinct: {field_info['distinct_values_count']}\n"
                        f"Nullable: {field_info['nullable_count']}"
                    ),
                    shape="box",
                    color="#f39c12"
                )
                net.add_edge(collection, field_node, color="#3498db")

        relationships = extract_relationships(schema)
        for ref_collection, refs in relationships.items():
            for ref in refs:
                if ref_collection in schema or ref_collection == "Unknown":
                    net.add_edge(
                        ref["from_collection"],
                        ref_collection,
                        label=f"Ref: {ref['field']}",
                        color="#e74c3c"
                    )

        net.show(output_file)
        logger.info(f"Schema visualization saved as '{output_file}'.")
    except Exception as e:
        logger.error(f"Error visualizing schema: {e}")

if __name__ == "__main__":
    parser = ArgumentParser(description="Analyze MongoDB schema from server or JSON file.")
    parser.add_argument('--host', default=DEFAULT_MONGODB_HOST, help='MongoDB host')
    parser.add_argument('--port', type=int, default=DEFAULT_MONGODB_PORT, help='MongoDB port')
    parser.add_argument('--database', help='MongoDB database name')
    parser.add_argument('--json', type=str, help='Path to JSON file containing MongoDB data')

    args = parser.parse_args()

    try:
        if args.json:
            with open(args.json, 'r') as file:
                json_data = json.load(file)
            schema, db_details = generate_schema_from_json(json_data)
        else:
            client = connect_to_db(args.host, args.port)
            if args.database:
                db = client[args.database]
                schema, db_details = generate_schema_for_db(db)
            else:
                logger.error("Database name is required when not using a JSON file.")
                raise ValueError("Database name not provided.")
        
        if schema:
            with open('schema.json', 'w') as f:
                json.dump({"db_details": db_details, "collections": schema}, f, indent=2)
            
            visualize_schema(schema, db_details)
        else:
            logger.error("Failed to generate schema.")
    except Exception as e:
        logger.error(f"An error occurred during the script execution: {e}")
    finally:
        if client:
            client.close()
            client = None
for collection, details in schema.items():
            collection_label = (
                f"{collection}\nDocuments: {details['total_documents']}\nAvg Size: "
                f"{details['avg_document_size']:.2f} KB\nSize: {details['collection_size']} bytes"
            )