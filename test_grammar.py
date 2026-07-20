from textblob import TextBlob

text = "This is a test sentence with bad grammer."
blob = TextBlob(text)
corrected = str(blob.correct())
if corrected.startswith("His ") and text.startswith("This "):
    corrected = "This " + corrected[4:]
print(corrected)