from sentence_transformers import SentenceTransformer, util
import torch

paraphraser = SentenceTransformer('sentence-transformers/paraphrase-MiniLM-L6-v2')
text = "The quick brown fox jumps over the lazy dog"
candidates = [
    text,
    text.replace("quick", "swift").replace("lazy", "idle"),
    text.replace("jumps", "leaps").replace("brown", "dark"),
    text.replace("dog", "hound").replace("over", "across")
]
embeddings = paraphraser.encode(candidates, convert_to_tensor=True)
cosine_scores = util.cos_sim(embeddings[0], embeddings[1:])[0]
best_idx = torch.argmax(cosine_scores).item() + 1
result = candidates[best_idx]
print(result)