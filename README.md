# ScholarSync

#### Video Demo: https://youtu.be/snK9Js9JEbw

## Overview

**ScholarSync** is a student-focused web application built with **Python**, **Flask**, and **SQLite** to help students discover academic and career opportunities, check how well they match eligibility requirements, save interesting listings, track application statuses, and stay ahead of approaching deadlines. Students often encounter internships, scholarships, fellowships, research roles, hackathons, and grants spread across different sources with varying requirements and closing dates. ScholarSync brings opportunity discovery, rule-based eligibility evaluation, bookmarking, application tracking, and deadline alerts into one central platform.

Rather than leaving students to manually compare every listing against their background, ScholarSync uses profile details—such as degree, academic year, field of study, skills, and interests—to evaluate eligibility and surface relevant opportunities alongside search and filter tools.

## Project Structure & File Breakdown

* **`app.py`**: The core Flask application handling routes, session-based authentication (registration, login, logout), profile and skill/interest updates, Smart Eligibility evaluation and ranking, `/explore` search and filtering, bookmarking, application status tracking, and deadline notifications.
* **`schema.sql`**: Defines the relational SQLite database schema, including tables for `users`, `opportunities`, `skills`, `user_skills`, `interests`, `user_interests`, `saved_opportunities`, `applications`, and `notifications`.
* **`seed.sql`**: Populates initial sample opportunity records so students have realistic listings to browse, filter, evaluate, save, and track.
* **`scholarsync.db`**: The local SQLite database file storing persistent application data across sessions.
* **`templates/base.html`**: The shared layout template defining the navigation bar, flash message display, styling, and footer across pages.
* **`templates/index.html`**: The landing page introducing ScholarSync and displaying the live count of available opportunities.
* **`templates/register.html` & `templates/login.html`**: Student registration and sign-in pages with form validation and session management.
* **`templates/dashboard.html`**: The authenticated student workspace displaying profile completion and profile/skills/interests editing, recommended opportunities, upcoming deadlines, saved bookmarks, tracked applications, and deadline alert notifications.
* **`templates/explore.html`**: Search and filter view supporting keyword queries, category and mode filters, academic year filters, paid/unpaid compensation filtering, eligibility filtering, and sorting.
* **`templates/opportunity_detail.html`**: Detailed view for an individual opportunity showing full listing information, Smart Eligibility match details, bookmark toggling, and application status tracking controls.

## Design Choices & Implementation Details

I chose **Flask** and **SQLite** because they keep the backend lightweight, transparent, and self-contained for a course project without requiring external database servers. Parameterized SQLite queries (`PRAGMA foreign_keys = ON`) and hashed passwords keep database reads and writes clean and user credentials protected.

I also designed the **Smart Eligibility** evaluation around clear, rule-based checks—comparing academic year range, target degree, required skills, and domain/interest alignment against listing attributes—so students can see *why* a listing matches or what profile details are missing, while still being able to explore all listings freely. Deadline countdowns and notifications are derived from real opportunity deadlines so saved opportunities approaching their due dates can alert the student.

## How to Run Locally

1. Open a terminal and move into the `scholarsync` folder:
   ```bash
   cd scholarsync

#AUTHOR AND CREDITS
NAME- Riddhi Verma
GitHub Username- veiddhi2107-glitch
Edx Username-veiddhi2107
City,Country- Bhubaneshwar, India
Date- November 24,2026
