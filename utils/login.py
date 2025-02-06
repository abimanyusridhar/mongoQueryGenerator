from datetime import datetime
from http import client
import json
import os
from uuid import uuid4
from venv import logger
from bson import ObjectId
import bson
from flask import app, flash, redirect, request, url_for, session, current_app
from flask_mail import Mail, Message
import requests
from werkzeug.security import generate_password_hash, check_password_hash
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, login_user, logout_user, current_user, login_required
from requests_oauthlib import OAuth2Session

from app import DATABASE_NAME, USER_COLLECTION_NAME, get_google_provider_cfg, get_mongo_client, log_action

# Initialize extensions
db = SQLAlchemy()
mail = Mail()
login_manager = LoginManager()

# Google OAuth setup
GOOGLE_CLIENT_ID = os.getenv('GOOGLE_CLIENT_ID')
GOOGLE_CLIENT_SECRET = os.getenv('GOOGLE_CLIENT_SECRET')
AUTHORIZATION_BASE_URL = 'https://accounts.google.com/o/oauth2/auth'
TOKEN_URL = 'https://accounts.google.com/o/oauth2/token'
SCOPE = ["openid", "profile", "email"]  # Added 'openid' for better security with Google
REDIRECT_URI = os.getenv('GOOGLE_REDIRECT_URI', 'http://localhost:5000/login/google/callback')

@login_manager.user_loader
def load_user(user_id):
    with get_mongo_client() as mongo_client:
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
                # If user_id isn't a valid ObjectId, return None or handle accordingly
                return None
        
        return User.from_mongo(mongo_user) if mongo_user else None

class User(db.Model):
    """User model for authentication and password recovery."""
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(128), nullable=False)
    security_question = db.Column(db.String(256), nullable=True)
    security_answer_hash = db.Column(db.String(128), nullable=True)
    google_id = db.Column(db.String(128), unique=True, nullable=True)  # For Google OAuth users

    def set_password(self, password):
        """Set the user's password after hashing."""
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        """Check if the given password matches the stored password hash."""
        return check_password_hash(self.password_hash, password)

    def set_security_answer(self, answer):
        """Set the security answer after hashing."""
        self.security_answer_hash = generate_password_hash(answer)

    def check_security_answer(self, answer):
        """Check if the given answer matches the stored security answer hash."""
        return check_password_hash(self.security_answer_hash, answer)

def init_app(app):
    """Initialize the Flask extensions with the app configuration."""
    app.config.update(
        MAIL_SERVER=os.getenv("MAIL_SERVER", "smtp.gmail.com"),
        MAIL_PORT=int(os.getenv("MAIL_PORT", 587)),
        MAIL_USE_TLS=True,
        MAIL_USERNAME=os.getenv("MAIL_USERNAME"),
        MAIL_PASSWORD=os.getenv("MAIL_PASSWORD"),
        MAIL_DEFAULT_SENDER=os.getenv("MAIL_DEFAULT_SENDER", "noreply@example.com"),
        SECRET_KEY=os.getenv('SECRET_KEY', os.urandom(24).hex()),
        SQLALCHEMY_DATABASE_URI=os.getenv("DATABASE_URL", "sqlite:///app.db"),
        SQLALCHEMY_TRACK_MODIFICATIONS=False
    )

    mail.init_app(app)
    login_manager.init_app(app)
    login_manager.login_view = 'login'
    db.init_app(app)

    # Google OAuth routes
    app.add_url_rule('/login/google', 'google_login', google_login)
    app.add_url_rule('/login/google/callback', 'google_callback', google_callback)

def create_user(email, password=None, security_question=None, security_answer=None, google_id=None):
    """Create a new user with a security question or Google ID."""
    if get_user_by_email(email):
        flash("User with this email already exists.", "danger")
        return None

    try:
        user = User(email=email, google_id=google_id)
        if password:
            user.set_password(password)
            user.security_question = security_question
            user.set_security_answer(security_answer)
        db.session.add(user)
        db.session.commit()
        current_app.logger.info(f"User created: {email}")
        flash("User successfully created!", "success")
        return user
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error creating user: {e}")
        flash("An error occurred during registration. Please try again.", "danger")
        return None

def get_user_by_email(email):
    """Retrieve a user by email."""
    return User.query.filter_by(email=email).first()

def update_user_password(user, new_password):
    """Update the password for a given user."""
    try:
        user.set_password(new_password)
        db.session.commit()
        current_app.logger.info(f"Password updated for user: {user.email}")
        flash("Your password has been updated successfully!", "success")
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error updating password: {e}")
        flash("Failed to update password. Please try again.", "danger")

def reset_password(email, security_answer, new_password):
    """Reset password using security question and answer."""
    user = get_user_by_email(email)
    if not user or not user.check_security_answer(security_answer):
        flash("Invalid email or security answer. Please try again.", "danger")
        return redirect(url_for('reset_password'))

    update_user_password(user, new_password)
    return redirect(url_for('login'))

def login_user_with_email(email, password):
    """Log in a user with email and password."""
    user = get_user_by_email(email)
    if user and user.check_password(password):
        login_user(user)
        flash("Logged in successfully!", "success")
        return redirect(url_for('select_db'))
    flash("Invalid email or password. Please try again.", "danger")
    return redirect(url_for('login'))

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
    with get_mongo_client() as mongo_client:
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


def logout_current_user():
    """Log out the current user."""
    logout_user()
    flash("You have been logged out.", "info")
    return redirect(url_for('login'))