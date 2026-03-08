-- INGESTION: tracks each repo ingestion run
DEFINE TABLE IF NOT EXISTS ingestion SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS repo_path    ON ingestion TYPE string;
DEFINE FIELD IF NOT EXISTS repo_name    ON ingestion TYPE string;
DEFINE FIELD IF NOT EXISTS github_url   ON ingestion TYPE option<string>;
DEFINE FIELD IF NOT EXISTS ingested_at  ON ingestion TYPE datetime;
DEFINE FIELD IF NOT EXISTS status       ON ingestion TYPE string DEFAULT 'pending';
DEFINE FIELD IF NOT EXISTS content_hash ON ingestion TYPE option<string>;
DEFINE FIELD IF NOT EXISTS file_count     ON ingestion TYPE option<int>;
DEFINE FIELD IF NOT EXISTS snapshot_path  ON ingestion TYPE option<string>;

-- REPO: represents the root of an indexed repository
DEFINE TABLE IF NOT EXISTS repo SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS path         ON repo TYPE string;
DEFINE FIELD IF NOT EXISTS name         ON repo TYPE string;
DEFINE FIELD IF NOT EXISTS ingestion_id ON repo TYPE string;

REMOVE INDEX IF EXISTS repo_path_idx ON TABLE repo;
DEFINE INDEX repo_path_iid_idx ON repo FIELDS path, ingestion_id UNIQUE;

-- FOLDER: represents a directory containing .py files
DEFINE TABLE IF NOT EXISTS folder SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS path         ON folder TYPE string;
DEFINE FIELD IF NOT EXISTS ingestion_id ON folder TYPE string;
DEFINE FIELD IF NOT EXISTS content_hash ON folder TYPE option<string>;

REMOVE INDEX IF EXISTS folder_path_idx ON TABLE folder;
DEFINE INDEX folder_path_iid_idx ON folder FIELDS path, ingestion_id UNIQUE;

-- FILE: represents a .py source file
DEFINE TABLE IF NOT EXISTS file SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS path         ON file TYPE string;
DEFINE FIELD IF NOT EXISTS language     ON file TYPE string DEFAULT 'python';
DEFINE FIELD IF NOT EXISTS line_count   ON file TYPE int;
DEFINE FIELD IF NOT EXISTS embedding    ON file TYPE option<array>;
DEFINE FIELD IF NOT EXISTS ingestion_id ON file TYPE string;
DEFINE FIELD IF NOT EXISTS content_hash ON file TYPE option<string>;
DEFINE FIELD IF NOT EXISTS diff_status  ON file TYPE option<string>;

REMOVE INDEX IF EXISTS file_path_idx ON TABLE file;
DEFINE INDEX file_path_iid_idx ON file FIELDS path, ingestion_id UNIQUE;

-- FUNCTION: a named function or method
DEFINE TABLE IF NOT EXISTS `function` SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS name          ON `function` TYPE string;
DEFINE FIELD IF NOT EXISTS file          ON `function` TYPE record<file>;
DEFINE FIELD IF NOT EXISTS lineno        ON `function` TYPE int;
DEFINE FIELD IF NOT EXISTS docstring     ON `function` TYPE option<string>;
DEFINE FIELD IF NOT EXISTS has_docstring ON `function` TYPE bool DEFAULT false;
DEFINE FIELD IF NOT EXISTS class_name    ON `function` TYPE option<string>;
DEFINE FIELD IF NOT EXISTS is_method     ON `function` TYPE bool DEFAULT false;
DEFINE FIELD IF NOT EXISTS embedding      ON `function` TYPE option<array>;
DEFINE FIELD IF NOT EXISTS ingestion_id   ON `function` TYPE string;
DEFINE FIELD IF NOT EXISTS content_hash   ON `function` TYPE option<string>;
DEFINE FIELD IF NOT EXISTS diff_status    ON `function` TYPE option<string>;
DEFINE FIELD IF NOT EXISTS source            ON `function` TYPE option<string>;
DEFINE FIELD IF NOT EXISTS suggested_docstring ON `function` TYPE option<string>;

REMOVE INDEX IF EXISTS fn_name_file_class_idx ON TABLE `function`;
DEFINE INDEX fn_name_file_class_iid_idx ON `function` FIELDS name, file, class_name, ingestion_id UNIQUE;

-- CLASS: a class definition
DEFINE TABLE IF NOT EXISTS `class` SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS name          ON `class` TYPE string;
DEFINE FIELD IF NOT EXISTS file          ON `class` TYPE record<file>;
DEFINE FIELD IF NOT EXISTS lineno        ON `class` TYPE int;
DEFINE FIELD IF NOT EXISTS bases         ON `class` TYPE array DEFAULT [];
DEFINE FIELD IF NOT EXISTS ingestion_id  ON `class` TYPE string;

REMOVE INDEX IF EXISTS class_name_file_idx ON TABLE `class`;
DEFINE INDEX class_name_file_iid_idx ON `class` FIELDS name, file, ingestion_id UNIQUE;

-- EDGE TABLES
DEFINE TABLE IF NOT EXISTS imports   SCHEMALESS;
DEFINE TABLE IF NOT EXISTS contains  SCHEMALESS;
DEFINE TABLE IF NOT EXISTS calls     SCHEMALESS;
DEFINE TABLE IF NOT EXISTS inherits  SCHEMALESS;
DEFINE TABLE IF NOT EXISTS in_folder SCHEMALESS;
DEFINE TABLE IF NOT EXISTS in_repo   SCHEMALESS;

DEFINE ANALYZER IF NOT EXISTS fn_analyzer TOKENIZERS class, punct FILTERS lowercase, ascii;
REMOVE INDEX IF EXISTS fn_name_ft ON TABLE `function`;
DEFINE INDEX fn_name_ft ON `function` FIELDS name FULLTEXT ANALYZER fn_analyzer BM25;
REMOVE INDEX IF EXISTS fn_docstring_ft ON TABLE `function`;
DEFINE INDEX fn_docstring_ft ON `function` FIELDS docstring FULLTEXT ANALYZER fn_analyzer BM25;
REMOVE INDEX IF EXISTS fn_generated_summary_ft ON TABLE `function`;
REMOVE INDEX IF EXISTS fn_suggested_docstring_ft ON TABLE `function`;
DEFINE INDEX fn_suggested_docstring_ft ON `function` FIELDS suggested_docstring FULLTEXT ANALYZER fn_analyzer BM25;
REMOVE INDEX IF EXISTS fn_embedding_hnsw ON TABLE `function`;
DEFINE INDEX fn_embedding_hnsw ON `function` FIELDS embedding HNSW DIMENSION 768 DIST COSINE;
