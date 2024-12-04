from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from utils.db_connection import connect_to_db, get_connection
from utils.schema_generator import generate_schema, get_schema_details
from transformers import pipeline
import os
import json
import re

# Initialize Flask application
app = Flask(__name__)
app.secret_key = 'your_secret_key'  # Secret key for session management

# Folder for uploaded databases
UPLOAD_FOLDER = 'uploaded_databases'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# NLP Model for MongoDB query generation
nlp_model = pipeline('text2text-generation', model="t5-large")

# Global variable to store selected database
selected_db = None

# Home Route
@app.route('/')
def index():
    """Render the homepage."""
    return render_template('index.html')

# Route to select and upload/connect to database
@app.route('/select_db', methods=['GET', 'POST'])
def select_db():
    """Allow user to upload/select a database."""
    global selected_db
    if request.method == 'POST':
        db_type = request.form.get('db_type')
        if db_type == 'mongodb':
            selected_db = {
                'type': 'mongodb',
                'host': request.form.get('host', 'localhost'),
                'port': int(request.form.get('port', 27017)),
                'database': request.form.get('database')
            }
            flash('MongoDB connected successfully!', 'success')
            return redirect(url_for('connect_database'))
        elif db_type == 'json':
            file = request.files.get('json_file')
            if file:
                file_path = os.path.join(UPLOAD_FOLDER, file.filename)
                file.save(file_path)
                selected_db = {'type': 'json', 'file_path': file_path}
                flash('JSON file uploaded successfully!', 'success')
                return redirect(url_for('connect_database'))
            else:
                flash('No file selected or invalid file format.', 'danger')
        else:
            flash('Unsupported database type selected.', 'danger')

    return render_template('select_db.html')

# Route to connect to the selected database
@app.route('/connect')
def connect_database():
    """Connect to the selected database and display the schema."""
    global selected_db
    if not selected_db:
        flash('No database selected. Please select a database first.', 'danger')
        return redirect(url_for('select_db'))

    # Handle MongoDB connection
    if selected_db['type'] == 'mongodb':
        if connect_to_db(selected_db):
            schema = generate_schema()
            tables, relationships = get_schema_details(schema)
            return render_template('schema.html', tables=tables, relationships=relationships)

    flash('Failed to connect to the database. Please check your credentials.', 'danger')
    return redirect(url_for('select_db'))

# Route to handle NLP queries and convert them to MongoDB query
@app.route('/nlp_query', methods=['GET', 'POST'])
def nlp_query():
    """Handle natural language queries and execute corresponding MongoDB query."""
    if request.method == 'POST':
        natural_language_query = request.form.get('nl_query')
        if not natural_language_query.strip():
            flash('Query cannot be empty.', 'danger')
            return redirect(url_for('nlp_query'))

        # Generate schema and MongoDB query
        schema = generate_schema()
        schema_metadata = get_schema_details(schema)[0]
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

def execute_query_with_mongo_query(mongo_query):
    """Execute the given MongoDB query and return results."""
    client = get_connection()
    try:
        # Assuming the database is selected and query is valid
        db = client.get_default_database()
        result = eval(mongo_query)  # Assuming query is valid and safe
        return result, None
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

# Regular Expression for Common Queries
RE_PATTERNS = {
    "greater_than": re.compile(r"(find|show|get) all (\w+) where (\w+) (is|are)? greater than (\d+)"),
    "equals": re.compile(r"(find|show|get) all (\w+) where (\w+) (is|are)? (\w+)"),
}

def generate_query_from_pattern(nl_query, schema_metadata):
    """Generate MongoDB query using regular expressions for common queries."""
    for query_type, pattern in RE_PATTERNS.items():
        match = pattern.search(nl_query.lower())
        if match:
            collection = match.group(2)
            field = match.group(3)
            if collection not in schema_metadata:
                raise ValueError(f"Collection '{collection}' not found in the database.")

            if field not in schema_metadata[collection]['fields']:
                raise ValueError(f"Field '{field}' not found in collection '{collection}'.")

            if query_type == "greater_than":
                return {"collection": collection, "filter": {field: {"$gt": int(match.group(5))}}}
            elif query_type == "equals":
                return {"collection": collection, "filter": {field: match.group(5)}}
    
    return None

# Start the Flask app
if __name__ == "__main__":
    app.run(debug=True)
