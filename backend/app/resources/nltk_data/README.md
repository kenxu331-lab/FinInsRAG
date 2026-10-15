# Minimal NLTK data

This directory vendors only the runtime data used by the project with
`nltk==3.10.0`:

- `tokenizers/punkt_tab/english/`: English Punkt sentence parameters.
- `corpora/wordnet.zip`: WordNet data used by `WordNetLemmatizer`.

Official sources:

- `https://raw.githubusercontent.com/nltk/nltk_data/gh-pages/packages/tokenizers/punkt_tab.zip`
  - SHA-256: `e57f64187974277726a3417ca6f181ec5403676c717672eef6a748a7b20e0106`
- `https://raw.githubusercontent.com/nltk/nltk_data/gh-pages/packages/corpora/wordnet.zip`
  - SHA-256: `cbda5ea6eef7f36a97a43d4a75f85e07fccbb4f23657d27b4ccbc93e2646ab59`

Only the English directory is extracted from `punkt_tab.zip`. WordNet remains
compressed because NLTK can read the corpus directly from the archive.
