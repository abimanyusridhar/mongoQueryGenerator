from pymongo import MongoClient
from pymongo.errors import ConnectionFailure
import os
import json

client = None  # Global MongoDB client object

def connect_to_db(db_details):
    """
    Connect to a MongoDB database or load data from an external .js MongoDB file.
    
    Parameters:
    db_details (dict): A dictionary containing connection details:
                       - 'type': must be 'mongodb'
                       - 'host': MongoDB host (default 'localhost')
                       - 'port': MongoDB port (default 27017)
                       - 'database': Name of the database to connect to
                       - 'file_path': Path to an external .js MongoDB file (optional)

    Returns:
    bool: True if connection is successful or file is processed successfully, False otherwise.
    """
    global client
    try:
        # Ensure we are connecting to a MongoDB database
        if db_details.get('type') != 'mongodb':
            raise ValueError("Unsupported database type. Only 'mongodb' is supported.")

        # Establish the connection to MongoDB
        host = db_details.get('host', 'localhost')
        port = db_details.get('port', 27017)
        client = MongoClient(host=host, port=port)
        print(f"Connected to MongoDB server at {host}:{port}")

        # Process an external .js file if provided in db_details
        file_path = db_details.get('file_path')
        if file_path:
            if not os.path.exists(file_path) or not file_path.endswith('.js'):
                raise ValueError("Invalid file path or file type. Only .js files are supported.")
            
            # If a file is provided, process it and load data into the MongoDB instance
            process_external_file(file_path, client)
            print(f"External database file '{file_path}' processed and imported successfully.")
        else:
            # Ensure a valid database is specified
            db_name = db_details.get('database')
            if not db_name:
                raise ValueError("Database name is required when connecting to MongoDB.")
            
            db = client[db_name]
            # Ping the MongoDB server to verify connection
            client.admin.command('ping')
            print(f"Connected to MongoDB database '{db_name}' successfully.")
        
        return True
    except (ConnectionFailure, ValueError) as e:
        print(f"Connection failed: {e}")
        return False
    except Exception as e:
        print(f"Unexpected error occurred: {e}")
        return False

def process_external_file(file_path, client):
    """
    Process and import data from an external .js MongoDB database file.
    
    Parameters:
    file_path (str): Path to the .js file.
    client (MongoClient): MongoDB client object.
    """
    try:
        # Open and read the content of the .js file
        with open(file_path, 'r', encoding='utf-8') as file:
            file_content = file.read().replace('db.', '').replace(');', '').strip()

            # Split and parse each collection's data based on ';' delimiter
            collections = [line.strip() for line in file_content.split(';') if line.strip()]
            for collection_data in collections:
                # Extract the collection name and the data to insert
                collection_name, json_data = collection_data.split('.insert(', 1)
                collection_name = collection_name.strip()
                json_data = json.loads(json_data.strip())

                # Insert data into the specified MongoDB collection
                client['external_db'][collection_name].insert_many(
                    json_data if isinstance(json_data, list) else [json_data]
                )
            print(f"Data from '{file_path}' successfully imported into 'external_db' database.")
    except json.JSONDecodeError as e:
        # Handle JSON parsing errors
        raise ValueError(f"Failed to parse JSON in the external file: {e}")
    except Exception as e:
        # Handle unexpected errors during file processing
        raise ValueError(f"Failed to process the external file: {e}")

def get_connection():
    """
    Returns the active MongoDB client.
    
    Returns:
    MongoClient: The MongoDB client object if connected, otherwise None.
    """
    if client:
        return client
    else:
        print("No active MongoDB connection.")
        return None
