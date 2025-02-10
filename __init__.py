from flask import Flask
from transformers import T5Tokenizer, T5ForConditionalGeneration
import logging

# Global variables for model
model = None
tokenizer = None

def load_model():
    global model, tokenizer
    try:
        model_name = "t5-small"  # or whatever model you're using
        tokenizer = T5Tokenizer.from_pretrained(model_name)
        model = T5ForConditionalGeneration.from_pretrained(model_name)
        model.eval()
        model.to('cpu')  # Use 'cuda' if you have GPU support
        logging.info("T5 model and tokenizer loaded successfully.")
    except Exception as e:
        logging.error(f"Failed to load T5 model: {e}")
        # Optionally, you can raise an exception here or handle it differently

# Call this when your app initializes
load_model()

# Your Flask app setup
app = Flask(__name__)
