from flask import Flask, render_template, request, redirect, url_for, flash, session
from utils.db_connection import connect_to_db, get_connection
from utils.schema_generator import generate_schema, get_schema_details
from transformers import pipeline
import os
import json
import re
import pymongo

# Initialize Flask application
app = Flask(__name__)
app.secret_key = 'your_secret_key'  # Secret key for session management

# Folder for uploaded databases
UPLOAD_FOLDER = 'uploaded_databases'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# NLP Model for MongoDB query generation
nlp_model = pipeline('text2text-generation', model="t5-large")

# Home Route
@app.route('/')
def index():
    """Render the homepage."""
    return render_template('index.html')

# Route to select and upload/connect to database
@app.route('/select_db', methods=['GET', 'POST'])
def select_db():
    """Allow user to upload/select a database."""
    if request.method == 'POST':
        print(f"Form submitted with db_type: {request.form.get('db_type')}")  # Log form data
        db_type = request.form.get('db_type')

        if db_type == 'mongodb':
            # Set up MongoDB connection settings
            selected_db = {
                'type': 'mongodb',
                'host': request.form.get('host', 'localhost'),
                'port': int(request.form.get('port', 27017)),
                'database': request.form.get('database')
            }
            session['selected_db'] = selected_db
            flash('MongoDB connected successfully!', 'success')
            return redirect(url_for('connect_database'))

        elif db_type == 'json':
            # Handle file upload for JSON database
            file = request.files.get('json_file')
            if file:
                file_path = os.path.join(UPLOAD_FOLDER, file.filename)
                file.save(file_path)
                session['selected_db'] = {'type': 'json', 'file_path': file_path}
                flash('JSON file uploaded successfully!', 'success')
                return redirect(url_for('connect_database'))
            else:
                flash('No file selected or invalid file format.', 'danger')
        else:
            flash('Unsupported database type selected.', 'danger')

    return render_template('select_db.html')

# Route to connect to the selected database and generate schema
@app.route('/connect', methods=['GET'])
def connect_database():
    """Connect to the selected database and display the schema."""
    selected_db = session.get('selected_db')
    if not selected_db:
        flash('No database selected. Please select a database first.', 'danger')
        return redirect(url_for('select_db'))

    # Generate schema
    try:
        if selected_db['type'] == 'mongodb':
            client = pymongo.MongoClient(selected_db['host'], selected_db['port'])
            db = client[selected_db['database']]
            collections = db.list_collection_names()
            if collections:
                schema = generate_schema()  # Generate schema dynamically
                tables, relationships = get_schema_details(schema)
                session['schema'] = schema  # Store schema in session for NLP query generation
                return render_template('schema.html', tables=tables, relationships=relationships)
            else:
                flash('No collections found in the selected database.', 'warning')

        elif selected_db['type'] == 'json':
            with open(selected_db['file_path'], 'r') as file:
                data = json.load(file)
            schema = generate_schema()  # Replace with your schema generation logic for JSON
            tables, relationships = get_schema_details(schema)
            session['schema'] = schema
            return render_template('schema.html', tables=tables, relationships=relationships)

    except Exception as e:
        flash(f"Failed to connect to the database: {str(e)}", 'danger')
        return redirect(url_for('select_db'))

# Route to handle NLP queries and convert them to MongoDB query
@app.route('/nlp_query', methods=['GET', 'POST'])
def nlp_query():
    """Handle natural language queries and execute corresponding MongoDB query."""
    selected_db = session.get('selected_db')
    schema_metadata = session.get('schema')

    if not selected_db or not schema_metadata:
        flash('No database connected. Please select and connect to a database first.', 'danger')
        return redirect(url_for('select_db'))

    if request.method == 'POST':
        natural_language_query = request.form.get('nl_query')
        if not natural_language_query.strip():
            flash('Query cannot be empty.', 'danger')
            return redirect(url_for('nlp_query'))

        # Generate MongoDB query from NLP input
        mongo_query = generate_mongo_query(natural_language_query, schema_metadata)
        if not mongo_query:
            flash('Failed to generate MongoDB query from the input.', 'danger')
            return redirect(url_for('nlp_query'))

        # Execute the MongoDB query
        result, error = execute_query_with_mongo_query(mongo_query)
        if error:
            flash(f"Error executing MongoDB query: {error}", 'danger')
            return redirect(url_for('nlp_query'))

        return render_template('result.html', result=result)

    return render_template('nlp_query.html')

# Helper functions

def execute_query_with_mongo_query(mongo_query):
    """Execute the given MongoDB query and return results."""
    selected_db = session.get('selected_db')
    if not selected_db:
        return None, 'No database selected.'

    try:
        client = pymongo.MongoClient(
            selected_db['host'], selected_db['port'])
        db = client[selected_db['database']]
        collection_name = mongo_query['collection']
        collection = db[collection_name]
        result = collection.find(mongo_query['filter'])
        return list(result), None
    except Exception as e:
        return None, str(e)

def generate_mongo_query(natural_language_query, schema_metadata):
    """Convert a natural language query to MongoDB query using NLP model."""
    try:
        schema_context = json.dumps(schema_metadata, indent=2)
        prompt = f"Schema:\n{schema_context}\n\nQuery: {natural_language_query}\n\nMongoDB Query:"
        result = nlp_model(prompt, max_length=200, num_return_sequences=1)
        mongo_query = result[0]['generated_text'].strip()

        if not validate_mongo_query(mongo_query):
            print(f"Invalid or unsafe MongoDB query generated: {mongo_query}")
            return None

        return mongo_query
    except Exception as e:
        print(f"Error in MongoDB query generation: {e}")
        return None

def validate_mongo_query(mongo_query):
    """Validate the generated MongoDB query for syntax and safety."""
    valid_methods = ('find', 'insert', 'update', 'delete', 'aggregate', 'count')
    if not any(mongo_query.lower().startswith(method) for method in valid_methods):
        return False

    malicious_patterns = [r'--', r';--', r';$', r'/\*', r'\*/', r'@@', r'xp_']
    if any(re.search(pattern, mongo_query, re.IGNORECASE) for pattern in malicious_patterns):
        return False

    if '$eval' in mongo_query or '$where' in mongo_query:
        return False

    return True

# Start the Flask app
if __name__ == "__main__":
    app.run(debug=True)
