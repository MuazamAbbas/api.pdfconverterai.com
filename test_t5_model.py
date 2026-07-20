from transformers import pipeline; summarizer = pipeline("text2text-generation", model="google/flan-t5-small", framework="pt"); print("Model loaded successfully")
