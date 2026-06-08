#!/usr/bin/env python3
"""Add a curated technical skills list to data/skill_matrix.json without removing existing entries.

Usage:
  python3 scripts/add_technical_skills.py

This will add skills (if missing) under the `skills` key with short descriptions.
"""
import json
from pathlib import Path
from utilities.skill_matrix import load, save

TECHNICAL_SKILLS = {
    "Python": "Backend language used in services and scripts",
    "TypeScript": "Frontend/backend typed JS used in web UI and services",
    "React": "Frontend framework used in web UI",
    "Streamlit": "Rapid UI built using Streamlit",
    "SQL": "Relational database knowledge (MSSQL/Postgres)",
    "Azure DevOps": "Work item and pipeline management in ADO",
    "Azure": "Azure platform skills (AKS, ACR, App Services)",
    "Docker": "Containerization and Docker image management",
    "CI/CD": "Continuous integration and delivery (pipelines)",
    "REST APIs": "Design and consumption of RESTful APIs",
    "Microservices": "Service-oriented architecture and deployment",
    "Authentication": "Auth systems (OAuth, tokens, AAD)",
    "Testing": "Automated testing frameworks (pytest, Selenium)",
    "Performance": "Performance tuning and profiling",
    "Database Design": "Schema design and migrations",
    "Logging & Monitoring": "App insights, logging and monitoring",
    "Release Engineering": "Release coordination and rollback strategies",
    "Frontend": "General frontend engineering (HTML/CSS/JS)",
    "Backend": "Server-side engineering and APIs",
    "Data Processing": "ETL, data pipelines and transformations",
}


def main():
    p = Path(__file__).resolve().parents[1] / "data" / "skill_matrix.json"
    data = load(p)
    skills = data.get("skills", {})
    added = 0
    for k, desc in TECHNICAL_SKILLS.items():
        if k not in skills:
            skills[k] = {"description": desc}
            added += 1
    data["skills"] = skills
    save(data, p)
    print(f"Added {added} technical skills to {p}")


if __name__ == '__main__':
    raise SystemExit(main())
