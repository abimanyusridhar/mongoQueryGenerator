from datetime import datetime, timedelta
from functools import wraps
import os
import logging
import json
from uuid import uuid4
import bson
from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify
from flask_login import LoginManager, UserMixin, login_user, logout_user, current_user, login_required
from flask_sqlalchemy import SQLAlchemy
from pymongo import MongoClient
from pymongo.errors import ServerSelectionTimeoutError, ConnectionFailure
import requests
from config import Config
from transformers import pipeline
from bson import ObjectId, DBRef
from flask_wtf import FlaskForm
from wtforms import StringField, PasswordField, SubmitField, SelectField, FileField
from wtforms.validators import DataRequired, Email, Length, EqualTo, Regexp
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.exceptions import BadRequest
from flask_session import Session
from dotenv import load_dotenv
from requests_oauthlib import OAuth2Session
from oauthlib.oauth2 import WebApplicationClient
from utils.schema_generator import generate_schema, generate_schemas_for_all_dbs, extract_relationships, infer_mongodb_type
from utils.db_connection import get_mongo_client_with_context, connect_to_db, close_connection
load_dotenv()

# Flask app setup
app = Flask(__name__)
app.config.from_object(Config)
app.secret_key = os.getenv('SECRET_KEY', os.urandom(24).hex())

# Initialize CSRF protection
from flask_wtf.csrf import CSRFProtect
csrf = CSRFProtect(app)

# Proxy configuration for HTTPS
app.wsgi_app = ProxyFix(
    app.wsgi_app, 
    x_for=1, 
    x_proto=1, 
    x_host=1, 
    x_prefix=1
)

# Database configuration
app.config['SQLALCHEMY_DATABASE_URI'] = "sqlite:///site.db"
db = SQLAlchemy(app)

# Session configuration
app.config['SESSION_TYPE'] = 'filesystem'
app.config['SESSION_PERMANENT'] = False
app.config['SESSION_USE_SIGNER'] = True
app.config['SESSION_FILE_DIR'] = os.path.join(os.getcwd(), 'flask_session')
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(minutes=30)
app.config['UPLOAD_FOLDER'] = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'uploads')
os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'
# Google OAuth configuration
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
GOOGLE_DISCOVERY_URL = "https://accounts.google.com/.well-known/openid-configuration"

# OAuth client setup
client = WebApplicationClient(GOOGLE_CLIENT_ID)

# Logging setup
def setup_logger():
    logger = logging.getLogger(__name__)
    logger.setLevel(logging.INFO)
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    file_handler = logging.FileHandler('app.log')
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger

logger = setup_logger()

# MongoDB connection setup
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017/")
DATABASE_NAME = os.getenv("DATABASE_NAME", "my_database")
USER_COLLECTION_NAME = os.getenv("USER_COLLECTION_NAME", "users")

# Decorators
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated:
            flash('Please log in to access this page.', 'info')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def log_action(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        logger.info(f"Executing function: {f.__name__}")
        return f(*args, **kwargs)
    return decorated_function

# User model for Flask-Login
class User(UserMixin):
    def __init__(self, email, name=None, profile_pic=None, _id=None):
        self.email = email
        self.id = str(_id) if _id else email
        self.name = name
        self.profile_pic = profile_pic

    @classmethod
    def from_mongo(cls, mongo_data):
        return cls(
            email=mongo_data['email'], 
            name=mongo_data.get('name'), 
            profile_pic=mongo_data.get('profile_pic'), 
            _id=mongo_data['_id']
        )

    def get_id(self):
        return self.id

# Flask-Login setup
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"
@login_manager.user_loader
def load_user(user_id):
    db_details = {'type': 'mongodb', 'host': 'localhost', 'port': 27017, 'database': DATABASE_NAME}
    with get_mongo_client_with_context(db_details) as mongo_client:
        db = mongo_client[DATABASE_NAME]
        user_collection = db[USER_COLLECTION_NAME]
        
        # Try to match by email first, which is a common case
        mongo_user = user_collection.find_one({"email": user_id})
        
        if not mongo_user:
            # Check if user_id looks like an ObjectId before converting
            try:
                object_id = ObjectId(user_id)
                mongo_user = user_collection.find_one({"_id": object_id})
            except bson.errors.InvalidId:
                return None
        
        return User.from_mongo(mongo_user) if mongo_user else None

# NLP model setup
nlp_model = pipeline("text2text-generation", model="t5-base")

def get_google_provider_cfg():
    return requests.get(GOOGLE_DISCOVERY_URL).json()

# Forms
class LoginForm(FlaskForm):
    email = StringField('Email', validators=[DataRequired(), Email()])
    password = PasswordField('Password', validators=[DataRequired()])
    submit = SubmitField('Login')

class RegistrationForm(FlaskForm):
    email = StringField('Email', validators=[DataRequired(), Email()])
    new_password = PasswordField('Password', validators=[
        DataRequired(), 
        Length(min=8), 
        Regexp(r'^(?=.*[A-Za-z])(?=.*\d)(?=.*[@$!%*?&#])[A-Za-z\d@$!%*?&#]{8,}$')
    ])
    confirm_password = PasswordField('Confirm Password', validators=[DataRequired(), EqualTo('new_password', message='Passwords must match')])
    security_question = SelectField('Security Question', 
                                    choices=[('pet', 'What was the name of your first pet?'), 
                                             ('mother_maiden', 'What is your mother\'s maiden name?'), 
                                             ('birth_city', 'In which city were you born?')],
                                    validators=[DataRequired()])
    security_answer = StringField('Security Answer', validators=[DataRequired()])
    submit = SubmitField('Register')

class ResetPasswordForm(FlaskForm):
    email = StringField('Email', validators=[DataRequired(), Email()])
    security_question = SelectField('Security Question', 
                                    choices=[('pet', 'What was the name of your first pet?'), 
                                             ('school', 'What was the name of your elementary school?'), 
                                             ('city', 'In which city were you born?'), 
                                             ('friend', 'What is your best friend\'s first name?'), 
                                             ('mother', 'What is your mother\'s maiden name?')],
                                    validators=[DataRequired()])
    security_answer = StringField('Security Answer', validators=[DataRequired()])
    new_password = PasswordField('New Password', validators=[
        DataRequired(), 
        Length(min=8), 
        Regexp(r'^(?=.*[A-Za-z])(?=.*\d)(?=.*[@$!%*?&#])[A-Za-z\d@$!%*?&#]{8,}$')
    ])
    confirm_password = PasswordField('Confirm New Password', validators=[DataRequired(), EqualTo('new_password', message='Passwords must match')])
    submit = SubmitField('Reset Password')

# Helper functions
def serialize_schema(schema):
    """Serialize schema and handle MongoDB-specific types."""
    serialized_schema = {}
    for collection, details in schema.items():
        serialized_schema[collection] = {
            "total_documents": details.get("total_documents", 0),
            "avg_document_size": details.get("avg_document_size", 0),
            "fields": details.get("fields", []),
            "field_details": {k: {"type": v['type'], "distinct_values_count": v.get('distinct_values_count', 0), "nullable_count": v.get('nullable_count', 0)} for k, v in details.get("field_details", {}).items()},
            "indexes": details.get("indexes", [])
        }
    return json.dumps(serialized_schema, indent=2, default=str)  

def generate_mongo_query(natural_language_query, schema):
    if not nlp_model:
        raise RuntimeError("NLP model is not available.")
    
    schema_context = json.dumps(schema, indent=2)
    prompt = f"Schema:\n{schema_context}\n\nQuery: {natural_language_query}\n\nMongoDB Query:"
    try:
        result = nlp_model(prompt, max_length=200, num_return_sequences=1)
        query = json.loads(result[0]['generated_text'].strip())
        if not isinstance(query, dict) or 'collection' not in query or 'filter' not in query:
            raise ValueError("Generated query is not in correct format")
        return query
    except (json.JSONDecodeError, ValueError) as e:
        logger.error(f"Error generating MongoDB query: {str(e)}")
        return None

def execute_query_with_mongo_query(mongo_query):
    selected_db = session.get('selected_db')
    if not selected_db or selected_db['type'] != 'mongodb':
        return None, 'No MongoDB connection details in session or incorrect database type.'

    try:
        with get_mongo_client_with_context(selected_db) as client:
            db = client[selected_db['database']]
            collection = db[mongo_query.get('collection', '')]
            
            if not collection:
                return None, 'Collection not found in the database.'
            
            query_filter = mongo_query.get('filter', {})
            results = list(collection.find(query_filter, limit=100))
            
            return [
                {key: (str(value) if isinstance(value, (ObjectId, DBRef)) else value) for key, value in doc.items()}
                for doc in results
            ], None
    except ServerSelectionTimeoutError:
        logger.error(f"Could not connect to MongoDB server at {MONGO_URI}")
        return None, "Failed to connect to MongoDB server."
    except ConnectionFailure:
        logger.error(f"Connection to MongoDB failed: {MONGO_URI}")
        return None, "Database connection failed."
    except Exception as e:
        logger.error(f"Unexpected error executing MongoDB query: {str(e)}")
        return None, f"An unexpected error occurred: {str(e)}"

# Routes
@app.route('/')
def index():
    if current_user.is_authenticated:
        return render_template('index.html', user=current_user)
    return render_template('index.html')

@app.route('/login', methods=['GET', 'POST'])
@log_action
def login():
    form = LoginForm()
    if form.validate_on_submit():
        email = form.email.data
        password = form.password.data
        db_details = {'type': 'mongodb', 'host': 'localhost', 'port': 27017, 'database': DATABASE_NAME}
        with get_mongo_client_with_context(db_details) as mongo_client:
            db = mongo_client[DATABASE_NAME]
            user = db[USER_COLLECTION_NAME].find_one({"email": email})
        if user and check_password_hash(user['password'], password):
            user_obj = User.from_mongo(user)
            login_user(user_obj)
            logger.info(f"User {email} logged in successfully.")
            flash('Login successful!', 'success')
            return redirect(url_for('select_db'))
        else:
            logger.warning(f"Failed login attempt for email: {email}")
            flash('Invalid email or password.', 'danger')
    return render_template('login.html', form=form)

@app.route("/google_login")
@log_action
def google_login():
    google_provider_cfg = get_google_provider_cfg()
    authorization_endpoint = google_provider_cfg["authorization_endpoint"]

    # Generate state manually using UUID
    state = str(uuid4())
    session["oauth_state"] = state

    request_uri = client.prepare_request_uri(
        authorization_endpoint,
        redirect_uri=request.base_url + "/callback",
        scope=["openid", "email", "profile"],
        state=state
    )
    return redirect(request_uri)

@app.route("/google_login/callback")
@log_action
def google_callback():
    # Validate state for CSRF protection
    stored_state = session.pop("oauth_state", None)
    if request.args.get("state") != stored_state:
        logger.error("State mismatch during Google OAuth callback")
        flash("Invalid state parameter. Please try again.", "danger")
        return redirect(url_for("login"))
    
    code = request.args.get("code")
    if not code:
        logger.error("No code received from Google OAuth")
        flash("Failed to authenticate with Google. Please try again.", "danger")
        return redirect(url_for("login"))

    # Fetch token
    token_endpoint = get_google_provider_cfg()["token_endpoint"]
    token_url, headers, body = client.prepare_token_request(
        token_endpoint,
        authorization_response=request.url,
        redirect_url=request.base_url,
        code=code
    )
    token_response = requests.post(
        token_url,
        headers=headers,
        data=body,
        auth=(GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET),
    )

    if token_response.status_code != 200:
        logger.error(f"Failed to fetch token: {token_response.text}")
        flash("Failed to get authentication token from Google. Please try again.", "danger")
        return redirect(url_for("login"))

    client.parse_request_body_response(json.dumps(token_response.json()))

    # Fetch user info
    userinfo_endpoint = get_google_provider_cfg()["userinfo_endpoint"]
    uri, headers, body = client.add_token(userinfo_endpoint)
    userinfo_response = requests.get(uri, headers=headers, data=body)

    if userinfo_response.status_code != 200:
        logger.error(f"Failed to fetch user info: {userinfo_response.text}")
        flash("Failed to retrieve user information from Google. Please try again.", "danger")
        return redirect(url_for("login"))

    if userinfo_response.json().get("email_verified"):
        unique_id = userinfo_response.json()["sub"]
        users_email = userinfo_response.json()["email"]
        picture = userinfo_response.json()["picture"]
        users_name = userinfo_response.json()["given_name"]
    else:
        flash("User email not available or not verified by Google.", "danger")
        return redirect(url_for("login"))

    # Check if user exists, if not, create
    db_details = {'type': 'mongodb', 'host': 'localhost', 'port': 27017, 'database': DATABASE_NAME}
    with get_mongo_client_with_context(db_details) as mongo_client:
        db = mongo_client[DATABASE_NAME]
        user = db[USER_COLLECTION_NAME].find_one({"email": users_email})
        if not user:
            db[USER_COLLECTION_NAME].insert_one({
                "email": users_email,
                "google_id": unique_id,
                "name": users_name,
                "profile_pic": picture,
                "created_at": datetime.utcnow()
            })
            logger.info(f"New user registered with email: {users_email}")

    # Log the user in
    user_obj = User(email=users_email, name=users_name, profile_pic=picture)
    login_user(user_obj)
    
    flash("Logged in successfully!", "success")
    return redirect(url_for('select_db'))

@app.route("/logout")
@login_required
@log_action
def logout():
    logout_user()
    session.pop('google_token', None)
    flash('You have been logged out.', 'info')
    return redirect(url_for('login'))

@app.route('/register', methods=['GET', 'POST'])
@log_action
def register():
    form = RegistrationForm()
    if form.validate_on_submit():
        email = form.email.data
        db_details = {'type': 'mongodb', 'host': 'localhost', 'port': 27017, 'database': DATABASE_NAME}
        with get_mongo_client_with_context(db_details) as mongo_client:
            db = mongo_client[DATABASE_NAME]
            if db[USER_COLLECTION_NAME].find_one({"email": email}):
                flash('Email already registered.', 'danger')
                return redirect(url_for('login'))
        
            hashed_password = generate_password_hash(form.new_password.data)
            new_user = {
                "email": email,
                "password": hashed_password,
                "security_question": form.security_question.data,
                "security_answer": generate_password_hash(form.security_answer.data),
                "created_at": datetime.utcnow()
            }
            db[USER_COLLECTION_NAME].insert_one(new_user)
        flash('Registration successful! Please login.', 'success')
        return redirect(url_for('login'))
    return render_template('register.html', form=form)

@app.route('/reset_password', methods=['GET', 'POST'])
@log_action
def reset_password():
    form = ResetPasswordForm()
    if form.validate_on_submit():
        db_details = {'type': 'mongodb', 'host': 'localhost', 'port': 27017, 'database': DATABASE_NAME}
        with get_mongo_client_with_context(db_details) as mongo_client:
            db = mongo_client[DATABASE_NAME]
            user = db[USER_COLLECTION_NAME].find_one({"email": form.email.data})
            if user and user['security_question'] == form.security_question.data:
                if check_password_hash(user['security_answer'], form.security_answer.data):
                    new_password_hash = generate_password_hash(form.new_password.data)
                    db[USER_COLLECTION_NAME].update_one({"email": form.email.data}, {"$set": {"password": new_password_hash}})
                    flash('Password reset successful!', 'success')
                    return redirect(url_for('login'))
                else:
                    flash('Incorrect security answer.', 'danger')
            else:
                flash('Email or security question not found.', 'danger')
    return render_template('reset_password.html', form=form)

@app.route('/select_db', methods=['GET', 'POST'])
@login_required
@log_action
def select_db():
    if request.method == 'POST':
        if 'csrf_token' not in request.form or not request.form['csrf_token']:
            flash('CSRF token missing or incorrect.', 'error')
            return redirect(url_for('select_db'))

        db_type = request.form.get('db_type')
        if db_type == 'mongodb':
            try:
                session['selected_db'] = {
                    'type': 'mongodb',
                    'host': request.form.get('host', 'localhost'),
                    'port': int(request.form.get('port', 27017)),
                    'database': request.form.get('database')
                }
                return redirect(url_for('connect_database'))
            except ValueError:
                flash('Invalid port number. Please enter a valid integer for the port.', 'error')
        elif db_type == 'json':
            file = request.files.get('db_file')
            if file and file.filename.lower().endswith('.json'):
                if 'UPLOAD_FOLDER' not in app.config:
                    logger.error("UPLOAD_FOLDER not set in app config")
                    flash("Server configuration error: UPLOAD_FOLDER not set.", 'error')
                    return redirect(url_for('select_db'))
                
                filename = secure_filename(file.filename)
                file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
                try:
                    file.save(file_path)
                    session['selected_db'] = {
                        'type': 'json',
                        'file_path': file_path
                    }
                    return redirect(url_for('connect_database'))
                except Exception as e:
                    logger.error(f"Error saving JSON file: {str(e)}")
                    flash(f"Error saving JSON file: {str(e)}", 'error')
            else:
                flash('Please upload a valid JSON file.', 'error')
        else:
            flash('Invalid database selection.', 'error')
    return render_template('select_db.html')

@app.route('/connect_database')
@login_required
@log_action
def connect_database():
    if 'selected_db' not in session:
        flash('No database selected.', 'error')
        return redirect(url_for('select_db'))
    
    selected_db = session['selected_db']
    try:
        if selected_db['type'] == 'mongodb':
            logger.info(f"Attempting to connect to MongoDB at {selected_db['host']}:{selected_db['port']}")
            with get_mongo_client_with_context(selected_db) as client:
                if client:
                    client.admin.command('ping')
                    logger.info("Successfully connected to MongoDB")
                    db = client[selected_db['database']]
                    schema = generate_schema(db)
                    
                    if not schema:
                        logger.error("Schema generation returned empty data.")
                        flash("Schema could not be generated properly.", 'error')
                        return redirect(url_for('select_db'))
                    
                    session['schema'] = schema
                    relationships = extract_relationships(schema)
                    session['relationships'] = relationships
                    db_details = {
                        'name': selected_db['database'],
                        'collections_count': len(schema),
                        'size_on_disk': db.command("dbstats").get("dataSize", 0)
                    }
                    session['db_details'] = db_details
                    return render_template('schema.html', schema=serialize_schema(schema), 
                                           relationships=relationships, db_details=db_details)
                else:
                    logger.error("Failed to connect to MongoDB.")
                    flash("Failed to connect to MongoDB.", 'error')
        elif selected_db['type'] == 'json':
            return handle_json_file(selected_db['file_path'])
    except Exception as e:
        logger.error(f"Error in connect_database: {str(e)}")
        flash(f"Error connecting to or processing the database: {str(e)}", 'error')
    return redirect(url_for('select_db'))

def handle_json_file(file_path):
    try:
        with open(file_path, 'r', encoding='utf-8') as file:
            data = json.load(file)
        
        if not isinstance(data, (dict, list)):
            raise ValueError(f"Expected JSON to be dict or list, got {type(data)}")

        schema = generate_json_schema(data)
        db_details = {
            'name': 'JSON File', 
            'collections_count': len(schema), 
            'size_on_disk': os.path.getsize(file_path)
        }
        session['schema'] = schema
        return render_template('schema.html', schema=serialize_schema(schema), db_details=db_details)
    
    except json.JSONDecodeError as e:
        logger.error(f"JSON file is not valid: {str(e)}")
        flash(f"JSON file is not valid: {str(e)}", 'error')
    except FileNotFoundError:
        logger.error("JSON file not found.")
        flash("The JSON file could not be found.", 'error')
    except ValueError as e:
        logger.error(f"Invalid JSON structure: {str(e)}")
        flash(f"Invalid JSON structure: {str(e)}", 'error')
    except Exception as e:
        logger.error(f"Error reading JSON file: {str(e)}")
        flash(f"Error reading JSON file: {str(e)}", 'error')
    return redirect(url_for('select_db'))

def generate_json_schema(data):
    if isinstance(data, dict):
        return {k: generate_collection_schema(v) for k, v in data.items() if v}
    elif isinstance(data, list):
        return {"default_collection": generate_collection_schema(data)}
    else:
        logger.warning(f"Unexpected data type in JSON: {type(data)}")
        return {}

def generate_collection_schema(collection_data):
    if not collection_data:
        return {
            'fields': [],
            'sample_data': [],
            'total_documents': 0,
            'avg_document_size': 0,
            'field_details': {},
            'indexes': []
        }
    
    if isinstance(collection_data, list):
        sample = next((item for item in collection_data if isinstance(item, dict)), None)
        if sample is None:
                     # If no dictionary found, use the first item or None
            return {
                'fields': [],
                'sample_data': [collection_data[0] if collection_data else None],
                'total_documents': len(collection_data),
                'avg_document_size': 0,  # Placeholder; actual size calculation omitted for simplicity
                'field_details': {},
                'indexes': []
            }
        else:
            sample = collection_data
            total_documents = 1

    if isinstance(sample, dict):
        return {
            'fields': list(sample.keys()),
            'sample_data': [sample],
            'total_documents': total_documents,
            'avg_document_size': 0,  # Placeholder; actual size calculation omitted for simplicity
            'field_details': {field: {"type": infer_mongodb_type(value), "distinct_values_count": 0, "nullable_count": 0} for field, value in sample.items()},
            'indexes': []
        }
    else:
        logger.warning(f"Sample data is not a dictionary: {type(sample)}")
        return {
            'fields': [],
            'sample_data': [sample],
            'total_documents': total_documents,
            'avg_document_size': 0,
            'field_details': {},
            'indexes': []
        }

@app.route('/nlp_query', methods=['POST'])
@login_required
@log_action
def nlp_query():
    if 'selected_db' not in session:
        flash('No database selected.', 'error')
        return redirect(url_for('select_db'))
    
    query = request.form.get('query')
    if not query:
        flash('Query cannot be empty.', 'error')
        return redirect(url_for('select_db'))
    
    try:
        mongo_query = generate_mongo_query(query, session['schema'])
        if mongo_query:
            results, error = execute_query_with_mongo_query(mongo_query)
            if error:
                flash(error, 'error')
            else:
                return render_template('query_results.html', results=results, query=query, mongo_query=mongo_query)
        else:
            flash('Failed to generate MongoDB query from natural language.', 'error')
        return redirect(url_for('select_db'))
    except Exception as e:
        logger.error(f"Error in nlp_query: {str(e)}")
        flash(f'An error occurred: {str(e)}', 'error')
        return redirect(url_for('select_db'))

@app.route('/execute_query', methods=['POST'])
@login_required
@log_action
def execute_query():
    try:
        query = request.json.get('query')
        if not query:
            return jsonify({"error": "No query provided."}), 400
        
        results, error = execute_query_with_mongo_query(query)
        if error:
            return jsonify({"error": error}), 400
        else:
            # Convert ObjectId to string for JSON serialization
            results = [{k: (str(v) if isinstance(v, ObjectId) else v) for k, v in doc.items()} for doc in results]
            return jsonify({"results": results})
    except Exception as e:
        logger.error(f"Error executing query: {str(e)}")
        return jsonify({"error": f"An error occurred while executing the query: {str(e)}"}), 500

if __name__ == "__main__":
    Session(app)
    app.run(debug=True)