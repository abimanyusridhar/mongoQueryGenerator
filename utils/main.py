import json
from db_connection import connect_to_db, close_connection
from schema_generator import generate_schema, generate_schemas_for_all_dbs, extract_relationships

if __name__ == "__main__":
    mongodb_details = {
        "type": "mongodb",
        "host": "localhost",
        "port": 27017,
        "database": "test_db"  # Optional for single database analysis
    }

    json_details = {
        "type": "json",
        "file_path": "example.json"
    }

    try:
        client = connect_to_db(mongodb_details)
        if client:
            print("MongoDB connection successful. Proceeding with further operations.")
            
            if 'database' in mongodb_details:
                db = client[mongodb_details["database"]]
                schema = generate_schema(db)
                if schema:
                    print(f"Generated schema for {mongodb_details['database']}:")
                    print(json.dumps(schema, indent=2))
                else:
                    print(f"Schema generation failed for {mongodb_details['database']}.")
            else:
                all_schemas = generate_schemas_for_all_dbs(client)
                if all_schemas:
                    for db_name, schema in all_schemas.items():
                        print(f"Schema for {db_name}:")
                        print(json.dumps(schema, indent=2))
                        relationships = extract_relationships(schema)
                        print(f"Relationships for {db_name}:")
                        print(json.dumps(relationships, indent=2))
                else:
                    print("Schema generation for all databases failed or no databases found.")
        else:
            print("Failed to connect to MongoDB.")

        # Process JSON file
        if connect_to_db(json_details) is None:
            print("JSON file processed successfully.")
        else:
            print("Failed to process the JSON file.")

    except Exception as e:
        print(f"An error occurred during script execution: {e}")
    finally:
        close_connection(client)