# NGO App — Donation Data & Certificate Automation (Prototype)

A Python-based prototype designed to automate donation data processing and fiscal certificate generation for non-profit organizations.

> **Status:** Prototype developed during a 180 Degrees Consulting project with Banco de Alimentos de Valencia. It was not deployed: the organization already had a management platform, so the final recommendation focused on making better use of it.
>
> **Note:** This project was built with AI assistance. I defined the requirements, designed the data model, and validated the system against real-world use cases.

---

## Problem

Non-profit organizations often manage donations using multiple Excel files coming from different sources and formats. This creates several operational challenges:

- Donor records may be duplicated or inconsistent.
- Donation data often requires manual cleaning before processing.
- Generating fiscal certificates manually is time-consuming and prone to errors.
- Limited traceability regarding when and how certificates were generated.

## Solution

The app provides an internal back-office tool that:

- Imports heterogeneous Excel donation datasets.
- Cleans and normalizes donor identity data (NIF / CIF / NIE).
- Deduplicates donor records.
- Stores donations using a relational data model.
- Generates fiscal certificates in PDF format.
- Records operational events for traceability.
- Supports multiple organizations (multi-tenant).

## Technology Stack

**Core technologies:** Python · Pandas · Streamlit · FastAPI · SQLite · ReportLab · OpenPyXL

**Concepts explored:** data cleaning and normalization, relational data modeling, idempotent processing, audit traceability, multi-tenant architecture.

## Project Structure

```text
NGO_app/
├── backend/            # Business logic and processing modules
├── app.py              # Main Streamlit application entry point
├── config.json         # System configuration
├── requirements.txt    # Python dependencies
├── env.example         # Template for environment variables
└── .gitignore          # Git exclusion rules
```

## Running the Application

**1. Clone the repository**

```bash
git clone https://github.com/dpastorsanluis/NGO_app.git
cd NGO_app
```

**2. Install dependencies**

```bash
pip install -r requirements.txt
```

**3. Configure environment variables**

Create a `.env` file based on `env.example` and fill in the required credentials.

**4. Run the application**

```bash
streamlit run app.py
```
