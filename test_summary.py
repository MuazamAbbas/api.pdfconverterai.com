from transformers import pipeline

summarizer = pipeline("summarization", model="t5-small")
text = "Artificial intelligence is transforming industries by automating tasks and providing insights from data analysis, but it also raises ethical concerns about privacy and job displacement."
result = summarizer(
    f"summarize: {text}",
    max_length=50,
    min_length=10,
    do_sample=False,
    num_beams=4,
    length_penalty=1.0
)[0]["summary_text"].strip()
print(result)