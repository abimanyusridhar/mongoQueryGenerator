import json
import os
import re
from typing import Any, Dict, Optional
import requests
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Retrieve Mistral API Key
MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY")
if not MISTRAL_API_KEY:
    raise ValueError("MISTRAL_API_KEY is not set in the environment")

MISTRAL_API_URL = "https://api.mistral.ai/v1/chat/completions"

# Set up headers for API requests
headers = {
    "Authorization": f"Bearer {MISTRAL_API_KEY}",
    "Content-Type": "application/json"
}

def generate_text_with_mistral(prompt: str, model: str = "mistral-large-latest") -> str:
    """
    Generate text using Mistral's API.

    :param prompt: The input prompt for the model.
    :param model: The Mistral model to use (e.g., 'mistral-large-latest').
    :return: Generated text from Mistral or an empty string if there's an error.
    """
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 512,
        "temperature": 0.7
    }
    try:
        response = requests.post(MISTRAL_API_URL, headers=headers, json=payload, timeout=60)
        response.raise_for_status()
        return response.json()['choices'][0]['message']['content'].strip()
    except requests.HTTPError as e:
        print(f"HTTP Error calling Mistral API: {e.response.status_code} - {e.response.text}")
        return ""
    except requests.RequestException as e:
        print(f"Error calling Mistral API: {e}")
        return ""
    except KeyError as e:
        print(f"Unexpected response format: {e}")
        return ""

def clean_json_output(text: str) -> str:
    """
    Clean up potential JSON from the text output by removing any text after valid JSON.

    :param text: The text potentially containing JSON.
    :return: A string that should be valid JSON or empty.
    """
    # Find the first valid JSON object and remove everything after it
    start = text.find('{')
    if start == -1:
        return ""
    end = text.rfind('}') + 1
    return text[start:end]

def generate_query_with_nlp(nl_query: str, schema: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Generate query using Mistral's API with additional checks."""
    try:
        schema_json = json.dumps(schema, default=str)
        prompt = f"""Given the following MongoDB schema and natural language query, generate a MongoDB query in JSON format:
        Schema: {schema_json}
        Query: {nl_query}
        Response Format: {{"collection": "...", "filter": {{...}}}}"""

        generated_text = generate_text_with_mistral(prompt)

        if not generated_text:
            print("No response from the model.")
            return None

        # Clean the output to get valid JSON
        cleaned_text = clean_json_output(generated_text)
        
        if not cleaned_text:
            print("No JSON found in the model's response.")
            return None

        try:
            result = json.loads(cleaned_text)
            if "collection" not in result or "filter" not in result:
                print("Generated query does not match expected format.")
                return None
            return result
        except json.JSONDecodeError:
            print("Generated text is not valid JSON after cleaning.")
            return None
    except Exception as e:
        print(f"NLP query generation failed: {e}")
        return None

def test_model_connection() -> bool:
    """Test if the API connection and model are working by sending a simple query."""
    test_prompt = "Hello, can you respond?"
    response = generate_text_with_mistral(test_prompt)
    return bool(response)  # Returns True if there's a response, False otherwise

if __name__ == "__main__":
    # Test the connection to the API
    if not test_model_connection():
        print("Failed to connect or receive response from Mistral AI. Check your API key and network connection.")
    else:
        print("Successfully connected to Mistral AI model.")
        
        test_prompt = "Find users where age is greater than 30"
        schema = {
            "users": {
                "fields": ["name", "age", "email"],
                "field_types": {"name": "string", "age": "int", "email": "string"}
            }
        }
        result = generate_query_with_nlp(test_prompt, schema)
        print(json.dumps(result, indent=2))