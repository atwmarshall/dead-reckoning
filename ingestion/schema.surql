-- FILE: represents a .py source file
DEFINE TABLE IF NOT EXISTS file SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS path       ON file TYPE string;
DEFINE FIELD IF NOT EXISTS language   ON file TYPE string DEFAULT 'python';
DEFINE FIELD IF NOT EXISTS line_count ON file TYPE int;
DEFINE FIELD IF NOT EXISTS embedding  ON file TYPE option<array>;

DEFINE INDEX IF NOT EXISTS file_path_idx ON file FIELDS path UNIQUE;

-- FUNCTION: a named function or method
DEFINE TABLE IF NOT EXISTS `function` SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS name         ON `function` TYPE string;
DEFINE FIELD IF NOT EXISTS file         ON `function` TYPE record<file>;
DEFINE FIELD IF NOT EXISTS lineno       ON `function` TYPE int;
DEFINE FIELD IF NOT EXISTS docstring    ON `function` TYPE option<string>;
DEFINE FIELD IF NOT EXISTS has_docstring ON `function` TYPE bool DEFAULT false;
DEFINE FIELD IF NOT EXISTS embedding    ON `function` TYPE option<array>;
DEFINE FIELD IF NOT EXISTS is_method    ON `function` TYPE bool DEFAULT false;

DEFINE INDEX IF NOT EXISTS fn_name_file_idx ON `function` FIELDS name, file UNIQUE;

-- CLASS: a class definition
DEFINE TABLE IF NOT EXISTS `class` SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS name   ON `class` TYPE string;
DEFINE FIELD IF NOT EXISTS file   ON `class` TYPE record<file>;
DEFINE FIELD IF NOT EXISTS lineno ON `class` TYPE int;
DEFINE FIELD IF NOT EXISTS bases  ON `class` TYPE array DEFAULT [];

DEFINE INDEX IF NOT EXISTS class_name_file_idx ON `class` FIELDS name, file UNIQUE;

-- EDGE TABLES
DEFINE TABLE IF NOT EXISTS imports  SCHEMALESS;
DEFINE TABLE IF NOT EXISTS contains SCHEMALESS;
DEFINE TABLE IF NOT EXISTS calls    SCHEMALESS;
DEFINE TABLE IF NOT EXISTS inherits SCHEMALESS;
