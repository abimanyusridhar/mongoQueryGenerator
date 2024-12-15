import os
from flask import flash, redirect, url_for, session, current_app
from flask_mail import Mail, Message
from werkzeug.security import generate_password_hash, check_password_hash
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from app import db, User  # Replace with your actual database and model import
from flask_login import login_user, logout_user

# Initialize Flask-Mail
mail = Mail()

def init_mail(app):
    """
    Initialize the Flask-Mail extension with app configuration.
    """
    app.config.update(
        MAIL_SERVER=os.getenv("MAIL_SERVER", "smtp.gmail.com"),
        MAIL_PORT=int(os.getenv("MAIL_PORT", 587)),
        MAIL_USE_TLS=True,
        MAIL_USERNAME=os.getenv("MAIL_USERNAME"),  # Your email
        MAIL_PASSWORD=os.getenv("MAIL_PASSWORD"),  # Your email password
        MAIL_DEFAULT_SENDER=os.getenv("MAIL_DEFAULT_SENDER", "noreply@example.com")
    )
    mail.init_app(app)

def send_email(subject, recipient, body):
    """
    Send an email using Flask-Mail.
    """
    try:
        msg = Message(subject=subject, recipients=[recipient], body=body)
        mail.send(msg)
        current_app.logger.info(f"Email sent successfully to {recipient}.")
    except Exception as e:
        current_app.logger.error(f"Error sending email: {e}")
        flash("Failed to send email. Please try again later.", "danger")
def generate_reset_token(email, expiration=3600):
    """
    Generate a secure reset token with an expiration time.
    """
    serializer = URLSafeTimedSerializer(current_app.secret_key)
    return serializer.dumps(email, salt="password-reset-salt")

def verify_reset_token(token, max_age=3600):
    """
    Verify the reset token and return the email if valid.
    """
    serializer = URLSafeTimedSerializer(current_app.secret_key)
    try:
        email = serializer.loads(token, salt="password-reset-salt", max_age=max_age)
        return email
    except SignatureExpired:
        flash("The reset token has expired. Please request a new one.", "danger")
    except BadSignature:
        flash("Invalid reset token.", "danger")
    return None
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(128), nullable=False)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

def get_user_by_email(email):
    """
    Retrieve a user by their email address.
    """
    return User.query.filter_by(email=email).first()

def create_user(email, password):
    """
    Create a new user and save them to the database.
    """
    try:
        hashed_password = generate_password_hash(password, method='bcrypt')
        user = User(email=email, password_hash=hashed_password)
        db.session.add(user)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error creating user: {e}")
        flash("An error occurred while creating the account.", "danger")

def update_user_password(user, new_password):
    """
    Update a user's password securely.
    """
    try:
        user.set_password(new_password)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error updating password: {e}")
        flash("Failed to reset the password. Please try again.", "danger")
def forgot_password(email):
    """
    Handle forgot password functionality.
    """
    user = get_user_by_email(email)
    if not user:
        flash("If the email exists, a reset link has been sent.", "info")
        return redirect(url_for('forgot_password'))

    reset_token = generate_reset_token(email)
    reset_url = url_for('reset_password', token=reset_token, _external=True)

    # Send reset email
    send_email(
        subject="Password Reset Request",
        recipient=email,
        body=(
            f"Please click the link below to reset your password:\n\n{reset_url}\n\n"
            "If you did not request this, please ignore this email."
        )
    )
    flash("If the email exists, a reset link has been sent.", "info")
    return redirect(url_for('login'))

def reset_password(token, new_password):
    """
    Handle password reset using a valid token.
    """
    email = verify_reset_token(token)
    if not email:
        return redirect(url_for('forgot_password'))

    user = get_user_by_email(email)
    if not user:
        flash("User not found.", "danger")
        return redirect(url_for('forgot_password'))

    update_user_password(user, new_password)
    flash("Your password has been reset successfully! Please log in.", "success")
    return redirect(url_for('login'))

def login_user(email, password):
    """
    Handle user login.
    """
    user = get_user_by_email(email)
    if user and user.check_password(password):
        session['user_id'] = user.id
        flash("Logged in successfully!", "success")
        login_user(user)
        return redirect(url_for('dashboard'))
    else:
        flash("Invalid email or password.", "danger")
        return redirect(url_for('login'))

def logout_user():
    """
    Handle user logout and clear session.
    """
    logout_user()
    flash("You have been logged out.", "info")
    return redirect(url_for('login'))
