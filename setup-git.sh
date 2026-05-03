#!/bin/bash
# One-shot git setup for the Oslo Apartment Hunt repo.
# Usage: bash setup-git.sh
# Stops at the first failure so you can see what went wrong.
set -e

echo "==> Cleaning any half-initialized .git/"
rm -rf .git

echo "==> git init"
git init -b main

echo "==> Setting commit identity for this repo"
git config user.name "Arnaud Dupuis"
git config user.email "arnaud.dupuis@farmforce.com"

echo "==> Adding remote"
git remote add origin https://github.com/arnaud-boop/oslo-apartment-hunt.git

echo "==> Staging files (respecting .gitignore)"
git add .

echo ""
echo "==> Files that will be committed:"
git status --short
echo ""

echo "==> Creating initial commit"
git commit -m "Initial v1: end-to-end pipeline with v1.0.5 enrichment"

echo ""
echo "==> Latest commit:"
git log --oneline -1
echo ""
echo "Local setup done. Next step (run this manually):"
echo "    git push -u origin main"
echo ""
echo "Push will prompt for your GitHub credentials (username + a personal"
echo "access token, or it'll use whatever credential helper macOS has set up)."
