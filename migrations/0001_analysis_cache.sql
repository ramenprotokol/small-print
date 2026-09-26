-- Analysis cache. One row per (document hash, pipeline version).
-- `analysis` is JSON holding character offsets, labels and one-line readings.
-- It never contains the document text: quotes are rebuilt from offsets
-- against the text the visitor sends.
CREATE TABLE IF NOT EXISTS analysis_cache (
  text_sha256 TEXT NOT NULL,
  pipeline    TEXT NOT NULL,
  analysis    TEXT NOT NULL,
  created_at  TEXT NOT NULL,
  PRIMARY KEY (text_sha256, pipeline)
);
