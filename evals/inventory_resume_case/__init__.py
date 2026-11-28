"""A self-contained crash/resume eval case: multi-batch inventory CSV -> side-effecting SQLite posting ->
per-SKU closing reconciliation CSV.

``prepare`` builds the interrupted scene a model resumes into (the last batch dies at one of two real
commit boundaries), ``check`` scores the books and the output file against the inbox ground truth,
``selfcheck`` runs both scenes end to end with scripted stand-ins — a mechanism check, never a model score.
"""
