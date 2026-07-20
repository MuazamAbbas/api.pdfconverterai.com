from transformers import pipeline; analyzer = pipeline("sentiment-analysis", model="distilbert-base-uncased-finetuned-sst-2-english"); print(analyzer("The stock market is booming"))
