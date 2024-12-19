import os
from flask import flash, redirect, url_for, session, current_app
from flask_mail import Mail, Message
from werkzeug.security import generate_password_hash, check_password_hash
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, login_user, logout_user, current_user, login_required
from flask_dance.contrib.google import make_google_blueprint, google

# Initialize extensions
db = SQLAlchemy()
mail = Mail()
login_manager = LoginManager()
google_blueprint = make_google_blueprint(
    client_id=os.getenv('GOOGLE_CLIENT_ID'),
    client_secret=os.getenv('GOOGLE_CLIENT_SECRET'),
    scope=["profile", "email"],
    redirect_to='google_authorized'
)

@login_manager.user_loader
def load_user(user_id):
    """Load a user from the database by user ID."""
    return User.query.get(int(user_id))

class User(db.Model):
    """User model for authentication and password recovery."""
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(128), nullable=False)
    security_question = db.Column(db.String(256), nullable=True)
    security_answer_hash = db.Column(db.String(128), nullable=True)

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
    # Configure Flask-Mail
    app.config.update(
        MAIL_SERVER=os.getenv("MAIL_SERVER", "smtp.gmail.com"),
        MAIL_PORT=int(os.getenv("MAIL_PORT", 587)),
        MAIL_USE_TLS=True,
        MAIL_USERNAME=os.getenv("MAIL_USERNAME"),
        MAIL_PASSWORD=os.getenv("MAIL_PASSWORD"),
        MAIL_DEFAULT_SENDER=os.getenv("MAIL_DEFAULT_SENDER", "noreply@example.com"),
    )
    mail.init_app(app)

    # Configure Flask-Login
    login_manager.init_app(app)
    login_manager.login_view = 'login'

    # Configure SQLAlchemy
    app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv("DATABASE_URL", "sqlite:///app.db")
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    db.init_app(app)

    # Register Google OAuth Blueprint
    app.register_blueprint(google_blueprint, url_prefix="/login")

def create_user(email, password, security_question, security_answer):
    """Create a new user with a security question."""
    try:
        # Check if the user already exists
        if get_user_by_email(email):
            flash("User with this email already exists.", "danger")
            return None

        user = User(
            email=email,
            security_question=security_question
        )
        user.set_password(password)
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
    flash("Your password has been reset successfully!", "success")
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

@google_blueprint.route("/google")
def google_login():
    """Redirect to Google's auth page for login."""
    if not current_user.is_authenticated:
        return redirect(url_for("google.login"))
    return redirect(url_for("select_db"))

@google_blueprint.route("/authorized")
def google_authorized():
    """Handle the response from Google after authentication."""
    if not google_blueprint.authorized:
        flash("You did not authorize the request", "danger")
        return redirect(url_for("login"))
    
    resp = google_blueprint.session.get("/userinfo")
    if not resp.ok:
        flash("Failed to fetch user info from Google", "danger")
        return redirect(url_for("login"))

    user_info = resp.json()
    email = user_info.get('email')
    user = get_user_by_email(email)
    
    if not user:
        # If user does not exist, create a new user with a default security question and answer
        user = create_user(email, "defaultpassword", "Default Question", "Default Answer")
        if user is None:
            return redirect(url_for("login"))

    login_user(user)
    flash(f"Logged in as {user.email}", "success")
    return redirect(url_for("select_db"))

def logout_current_user():
    """Log out the current user."""
    logout_user()
    flash("You have been logged out.", "info")
    return redirect(url_for('login'))