# Changelog
All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).


## [Unreleased]

## [0.2.1] - 2026-09-25

### Fixed

- Added the missing `py.typed` marker (PEP 561) - external type checkers were silently ignoring all of this package's type hints

## [0.2.0] - 2026-09-24

### Fixed

- `compile --rpm` now installs the database at `/etc/libmagic-wheel/<name>.mgc` instead of always `combined.mgc`, regardless of `--name`

### Added

- `compile --rpm-name` to set the RPM package name independently of `--name`
- `compile --install-path` to set the RPM's install path independently of `--name`
- `diff` subcommand: side-by-side comparison of override files against the bundled Magdir fragments they'd replace

## [0.1.0] - 2026-09-23

### Added

- Initial release: `Magic`, `Classification`, `compile_database`, `bundled_default_mgc`, `bundled_magdir`
- `libmagic-wheel` CLI: `compile` (with optional `--rpm`), `classify` subcommands

[Unreleased]: https://github.com/manfred-kaiser/libmagic-wheel/compare/0.2.1...main
[0.2.1]: https://github.com/manfred-kaiser/libmagic-wheel/compare/0.2.0...0.2.1
[0.2.0]: https://github.com/manfred-kaiser/libmagic-wheel/compare/0.1.0...0.2.0
[0.1.0]: https://github.com/manfred-kaiser/libmagic-wheel/releases/tag/0.1.0
