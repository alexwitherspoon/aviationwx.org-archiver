# Contributing to AviationWX.org Archiver

Thank you for your interest in contributing to the AviationWX.org Archiver! This document provides guidelines and instructions for contributing.

## Code of Conduct

This project adheres to a Code of Conduct that all contributors are expected to follow. Please read [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) before participating.

## Getting Started

1. **Fork the repository** on GitHub
2. **Clone your fork** locally:
   ```bash
   git clone https://github.com/YOUR_USERNAME/aviationwx.org-archiver.git
   cd aviationwx.org-archiver
   ```
3. **Set up local development** (Python 3.12+):
   ```bash
   # Copy the example config
   cp config/config.yaml.example config/config.yaml

   # Install dependencies
   make setup

   # Start with Docker
   make up
   ```

## How to Contribute

### Reporting Bugs

1. **Check existing issues** to see if the bug is already reported
2. **Create a new issue** with:
   - Clear title and description
   - Steps to reproduce
   - Expected vs actual behavior
   - Environment details (Docker version, OS, etc.)
   - Error messages or logs (without sensitive data)

### Suggesting Features

1. **Check existing issues** for similar suggestions
2. **Create a feature request** with:
   - Use case and motivation
   - Proposed solution or implementation ideas
   - Any related issues

### Code Contributions

1. **Create a branch** for your changes:
   ```bash
   git checkout -b feature/your-feature-name
   # or
   git checkout -b fix/your-bug-fix
   ```

2. **Make your changes** following our coding standards:
   - Follow [CODE_STYLE.md](CODE_STYLE.md)
   - Add concise comments only for critical or unclear logic
   - Write tests for all new functionality
   - Update documentation for user-facing changes
   - Write clear commit messages
   - **Do not commit** AI-generated temp files (research, analysis, plans)

3. **Test your changes** (required before every commit):
   ```bash
   make test-ci
   ```

4. **Commit your changes**:
   ```bash
   git add .
   git commit -m "Description of your changes"
   ```

5. **Push and create a Pull Request**:
   ```bash
   git push origin feature/your-feature-name
   ```
   Then create a PR on GitHub with:
   - Clear title and description
   - Reference related issues
   - Screenshots if UI changes
   - Testing notes

## Coding Standards

- Follow PEP 8 Python style guidelines
- Use meaningful variable and function names
- Keep functions focused and single-purpose
- Comments should be concise — only comment critical or unclear logic
- Write docstrings for public functions and classes
- Maintain minimal dependencies

### Security Guidelines

- **Never commit sensitive data** (API keys, passwords, credentials)
- Use `config/config.yaml.example` as a template
- Validate all user input

### Documentation

- Update relevant documentation files for user-facing changes
- Add inline comments for complex logic
- Update README.md if adding new features
- Keep code examples in documentation accurate

## Pull Request Process

1. **Ensure your code works** and doesn't break existing functionality
2. **Run tests**: `make test-ci` (lint + format check + tests)
3. **Update documentation** for any changes that affect users or developers
4. **Keep commits focused** — one logical change per commit
5. **Write clear commit messages**:
   ```
   Short summary (50 chars or less)

   More detailed explanation if needed. Wrap at 72 characters.
   Explain what and why vs. how.
   ```

6. **Respond to feedback** promptly and professionally
7. **Wait for review** before merging (even if you have write access)

## Questions?

- Open an issue for questions or discussions
- Check existing documentation first
- Be respectful and constructive in all communications

Thank you for contributing to AviationWX.org Archiver! 🛩️
