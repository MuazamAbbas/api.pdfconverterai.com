from rake_nltk import Rake; r = Rake(); text = "This is a sample text for keyword extraction"; r.extract_keywords_from_text(text); print(r.get_ranked_phrases()[:5])
