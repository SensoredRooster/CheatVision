PRAGMA foreign_keys = ON;

CREATE TABLE users (
  user_id TEXT PRIMARY KEY,
  email TEXT NOT NULL UNIQUE COLLATE NOCASE,
  role TEXT NOT NULL CHECK (role IN ('admin', 'tester')),
  password_salt TEXT,
  password_hash TEXT,
  password_iterations INTEGER NOT NULL DEFAULT 210000,
  is_active INTEGER NOT NULL DEFAULT 0 CHECK (is_active IN (0, 1)),
  auth_version INTEGER NOT NULL DEFAULT 1,
  created_at INTEGER NOT NULL,
  CHECK ((password_salt IS NULL) = (password_hash IS NULL))
);

CREATE UNIQUE INDEX users_single_admin ON users(role) WHERE role = 'admin';

CREATE TABLE auth_tokens (
  token_hash TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
  purpose TEXT NOT NULL CHECK (purpose IN ('invite', 'reset')),
  expires_at INTEGER NOT NULL,
  used_at INTEGER,
  consumed_nonce TEXT,
  created_at INTEGER NOT NULL
);

CREATE INDEX auth_tokens_user_purpose ON auth_tokens(user_id, purpose, created_at);

CREATE TABLE sessions (
  token_hash TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
  auth_version INTEGER NOT NULL,
  expires_at INTEGER NOT NULL,
  created_at INTEGER NOT NULL
);

CREATE INDEX sessions_expiry ON sessions(expires_at);
