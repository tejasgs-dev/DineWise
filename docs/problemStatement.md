# DineWise — Problem Statement

## AI-Powered Restaurant Recommendation System (Zomato Use Case)

DineWise is an AI-powered restaurant recommendation service inspired by Zomato. It suggests restaurants based on user preferences by combining structured restaurant data with a Large Language Model (LLM).

## Objective

Design and implement an application that:

- Takes user preferences such as location, budget, cuisine, and rating
- Uses a real-world dataset of restaurants
- Uses an LLM to generate personalized, human-like recommendations
- Shows the results to the user in a clear, useful format

## System Workflow

```
User Input ──► Data Filtering ──► LLM Prompt ──► Ranked Recommendations ──► Output Display
                    ▲
                    │
             Zomato Dataset (Hugging Face)
```

### 1. Data Ingestion

- Load and preprocess the Zomato dataset from Hugging Face:
  [ManikaSaini/zomato-restaurant-recommendation](https://huggingface.co/datasets/ManikaSaini/zomato-restaurant-recommendation)
- Extract the relevant fields:
  - Restaurant name
  - Location
  - Cuisine
  - Cost
  - Rating
  - Other useful attributes

### 2. User Input

Collect the user's preferences:

| Preference | Example values |
|---|---|
| Location | Delhi, Bangalore |
| Budget | Low, Medium, High |
| Cuisine | Italian, Chinese |
| Minimum rating | e.g. 4.0 |
| Additional preferences | Family-friendly, quick service |

### 3. Integration Layer

- Filter the restaurant data based on the user's input
- Pass the filtered, structured results into an LLM prompt
- Design the prompt so the LLM can reason about and rank the options

### 4. Recommendation Engine

Use the LLM to:

- Rank the candidate restaurants
- Explain why each recommendation fits the user's preferences
- Optionally summarize the choices

### 5. Output Display

Present the top recommendations in a user-friendly format, showing for each:

- Restaurant name
- Cuisine
- Rating
- Estimated cost
- AI-generated explanation
