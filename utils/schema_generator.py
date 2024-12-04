from pymongo import MongoClient
from collections import defaultdict
from pyvis.network import Network
import json
import os

client = None  # Global MongoDB client object


def connect_to_db(db_details: dict):
    global client
    try:
        if db_details.get("type") != "mongodb":
            raise ValueError("Only MongoDB is supported.")

        host = db_details.get("host", "localhost")
        port = db_details.get("port", 27017)
        client = MongoClient(host=host, port=port)

        # Ping the MongoDB server to ensure it's accessible
        client.admin.command("ping")
        print(f"Connected to MongoDB server at {host}:{port}")

        db_name = db_details.get("database")
        if not db_name:
            raise ValueError("Database name is required.")

        db = client[db_name]
        print(f"Connected to MongoDB database '{db_name}' successfully.")
        return db
    except Exception as e:
        print(f"Connection failed: {e}")
        return None


def process_external_file(file_path: str, db):
    try:
        with open(file_path, "r", encoding="utf-8") as file:
            file_content = file.read()
            file_content = file_content.replace("db.", "").replace(");", "").strip()
            collections = [
                line.strip() for line in file_content.split(";") if line.strip()
            ]

            for collection_data in collections:
                if ".insert(" in collection_data:
                    collection_name, json_data = collection_data.split(".insert(", 1)
                    collection_name = collection_name.strip()
                    json_data = json.loads(json_data.strip())

                    collection = db[collection_name]
                    collection.insert_many(
                        json_data if isinstance(json_data, list) else [json_data]
                    )
                    print(
                        f"Data from '{file_path}' successfully imported into '{collection_name}' collection."
                    )
    except json.JSONDecodeError as e:
        print(f"Failed to parse JSON in the external file: {e}")
    except Exception as e:
        print(f"Failed to process external file: {e}")


def generate_schema(db):
    try:
        schema = {}
        for collection_name in db.list_collection_names():
            collection = db[collection_name]
            sample_data = collection.find_one() or {}
            schema[collection_name] = {
                "fields": list(sample_data.keys()),
                "sample": sample_data,
            }
        print("Schema generation successful.")
        return schema
    except Exception as e:
        print(f"Error generating schema: {e}")
        return None


def get_schema_details(schema: dict):
    collections = {}
    relationships = defaultdict(list)
    try:
        for collection_name, details in schema.items():
            fields = details.get("fields", [])
            sample = details.get("sample", {})
            field_types = {field: type(value).__name__ for field, value in sample.items()}
            collections[collection_name] = {"fields": fields, "field_types": field_types}

            for field, value in sample.items():
                if isinstance(value, dict) and "$ref" in value and "$id" in value:
                    ref_collection = value.get("$ref")
                    relationships[ref_collection].append(
                        {
                            "from_collection": collection_name,
                            "field": field,
                            "referenced_id": value.get("$id"),
                        }
                    )
        print("Schema details extraction successful.")
        return collections, dict(relationships)
    except Exception as e:
        print(f"Error extracting schema details: {e}")
        return collections, dict(relationships)


def visualize_schema(schema: dict, relationships: dict):
    net = Network(height="750px", width="100%", directed=True)
    try:
        for collection, details in schema.items():
            net.add_node(
                collection, label=collection, shape="ellipse", color="#76c7c0"
            )
            for field, field_type in details["field_types"].items():
                field_node = f"{collection}.{field}"
                net.add_node(
                    field_node,
                    label=f"{field} ({field_type})",
                    shape="box",
                    color="#f39c12",
                )
                net.add_edge(collection, field_node, color="#3498db")

        for ref_collection, refs in relationships.items():
            for ref in refs:
                net.add_edge(
                    ref["from_collection"],
                    ref_collection,
                    label=f"Ref: {ref['field']}",
                    color="#e74c3c",
                )

        output_file = "schema_visualization.html"
        net.show(output_file)
        print(f"Schema visualization saved as '{output_file}'.")
    except Exception as e:
        print(f"Error visualizing schema: {e}")


# Main Flow
if __name__ == "__main__":
    db_details = {
        "type": "mongodb",
        "host": "localhost",
        "port": 27017,
        "database": "test_db",
    }
    db = connect_to_db(db_details)
    if db:
        schema = generate_schema(db)
        if schema:
            collections, relationships = get_schema_details(schema)
            visualize_schema(collections, relationships)
