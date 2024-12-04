from pymongo import MongoClient
from utils.db_connection import get_connection
from collections import defaultdict
from pyvis.network import Network
import json

def generate_schema():
    """
    Analyze and extract the MongoDB schema dynamically.

    Returns:
        dict: Schema details including collection names, fields, and sample data.
    """
    client = get_connection()
    if not client:
        print("Error: MongoDB connection is not available.")
        return None

    try:
        db = client.get_default_database()  # Get the default database
        schema = {}

        # Iterate through all collections in the database
        for collection_name in db.list_collection_names():
            collection = db[collection_name]
            # Analyze the first few documents to infer schema
            sample_data = collection.find_one() or {}
            schema[collection_name] = {
                'fields': list(sample_data.keys()),  # Field names
                'sample': sample_data                # Sample document
            }

        print("Schema generation successful.")
        return schema
    except Exception as e:
        print(f"Error generating schema: {e}")
        return None


def get_schema_details(schema):
    """
    Extract detailed schema information including fields, types, and inferred relationships.

    Args:
        schema (dict): MongoDB schema returned by `generate_schema`.

    Returns:
        tuple:
            - dict: Collection details including fields and inferred types.
            - dict: Relationships inferred between collections (if any).
    """
    collections = {}
    relationships = defaultdict(list)

    if not schema:
        print("Error: Schema is not available.")
        return collections, relationships

    try:
        # Process each collection
        for collection_name, details in schema.items():
            fields = details.get('fields', [])
            sample = details.get('sample', {})

            # Infer data types from sample data
            field_types = {field: type(value).__name__ for field, value in sample.items()}

            # Add collection details
            collections[collection_name] = {
                'fields': fields,
                'field_types': field_types
            }

            # Infer relationships (basic reference detection by convention)
            for field, value in sample.items():
                if isinstance(value, dict) and '$ref' in value and '$id' in value:
                    ref_collection = value.get('$ref')
                    if ref_collection:
                        relationships[ref_collection].append({
                            'from_collection': collection_name,
                            'field': field,
                            'referenced_id': value.get('$id')
                        })

        print("Schema details extraction successful.")
        return collections, dict(relationships)

    except Exception as e:
        print(f"Error extracting schema details: {e}")
        return collections, dict(relationships)


def visualize_schema(schema, relationships):
    """
    Create a visual representation of the MongoDB schema using PyVis.

    Args:
        schema (dict): Collection details with fields and data types.
        relationships (dict): Relationships inferred between collections.
    """
    net = Network(height="750px", width="100%", directed=True)

    # Add nodes for each collection
    for collection, details in schema.items():
        net.add_node(collection, label=collection, shape='ellipse', color='#76c7c0')

        # Add fields as child nodes
        for field, field_type in details['field_types'].items():
            field_node = f"{collection}.{field}"
            net.add_node(field_node, label=f"{field} ({field_type})", shape='box', color='#f39c12')
            net.add_edge(collection, field_node, color="#3498db")

    # Add edges for relationships (reference connections)
    for ref_collection, refs in relationships.items():
        for ref in refs:
            from_collection = ref['from_collection']
            # Relationship from one collection to another
            net.add_edge(from_collection, ref_collection, label=f"Ref: {ref['field']}", color='#e74c3c')

    # Generate interactive visualization
    output_file = "schema_visualization.html"
    net.show(output_file)
    print(f"Schema visualization saved as '{output_file}'.")


# Main Flow
if __name__ == "__main__":
    # Generate the schema from the MongoDB database
    schema = generate_schema()
    
    if schema:
        # Extract details and relationships from the schema
        collections, relationships = get_schema_details(schema)
        
        # Visualize the schema and relationships using PyVis
        visualize_schema(collections, relationships)
    else:
        print("Failed to generate schema. Please check the database connection.")
