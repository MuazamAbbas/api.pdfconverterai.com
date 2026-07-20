from transformers import pipeline; paraphraser = pipeline("text2text-generation", model="t5-small", framework="pt"); print("Model loaded successfully")
