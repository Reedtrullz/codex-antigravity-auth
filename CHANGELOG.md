# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.8.1] - 2026-08-20

### Added
- Per-account token refresh locking to prevent concurrent refresh attempts
- Explicit VALIDATION_REQUIRED error detection and messaging (auth issues no longer misreported as rate limits)
- Project discovery logging for better debugging of account setup issues
- Error classification helpers (classify_backend_status, is_validation_required_error)
- CHANGELOG.md

### Changed
- Improved error messages for VALIDATION_REQUIRED errors now include re-auth instructions
- Project discovery failures during token refresh are now logged instead of silently swallowed

## [1.8.0] - 2026-08-20

### Fixed
- Resolved HTTP 403 VALIDATION_REQUIRED errors from Google Cloud Code Assist API
- Switched to correct endpoint: daily-cloudcode-pa.googleapis.com
- Fixed User-Agent fingerprint to match real Antigravity IDE (antigravity/ide/2.5.5)
- Removed extra headers (X-Goog-Api-Client, Client-Metadata) that caused fingerprint mismatches
- Added project discovery via loadCodeAssist/onboardUser during login and token refresh

### Added
- Gemini 3.7 Flash (tiered wire ID with thinkingLevel support)
- Gemini 3.1 Flash Image generation model
- GPT-OSS 120B medium model
- Backward-compatible model aliases for retired Flash generations
- Anti skill updated with new model catalog and capabilities

### Changed
- Model catalog now matches current Antigravity API (7 models)
- Default model changed to claude-sonnet-4-6
- Context windows updated: 1M for Gemini, 250K for Claude

### Deprecated
- Gemini 3.5 Flash (routes to Gemini 3.7 Flash via backward-compat alias)
- Gemini 3.6 Flash (routes to Gemini 3.7 Flash via backward-compat alias)

## [1.7.0] - 2026-08-16

### Fixed
- Anti panel fallback identity and truthful multi-model consensus
- Installed skill sync and shipped PR workflow

### Added
- Diversity accounting for actual provider/model identities
- Fallback evidence even when fail-closed

## [1.6.0] - 2026-08-14

### Fixed
- Deep LP Tracker audit and remediation
- Trusted refresh and rerun commands
- FIFO tax lots and XIRR calculations
