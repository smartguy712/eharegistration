-- ============================================================
-- HANDBALL REGISTRATION & MANAGEMENT SYSTEM
-- Database Schema - PostgreSQL
-- Target: Eswatini (SZL / E)
-- Version: 1.0.0
-- ============================================================

-- 1. Core Teams Table
CREATE TABLE IF NOT EXISTS teams (
    team_id SERIAL PRIMARY KEY,
    team_name VARCHAR(100) NOT NULL UNIQUE,
    primary_jersey_color VARCHAR(30),
    secondary_jersey_color VARCHAR(30),
    manager_name VARCHAR(100) NOT NULL,
    manager_phone VARCHAR(30),
    team_fee_status VARCHAR(20) DEFAULT 'PENDING', -- PENDING, PAID
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 2. Master Permanent Players Table
CREATE TABLE IF NOT EXISTS players (
    player_id SERIAL PRIMARY KEY,
    global_license_number VARCHAR(50) UNIQUE NOT NULL,
    legal_name VARCHAR(150) NOT NULL,
    date_of_birth DATE NOT NULL,
    gender VARCHAR(10) NOT NULL,
    blood_type VARCHAR(5),
    emergency_contact_name VARCHAR(100),
    emergency_contact_phone VARCHAR(30),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 3. Seasonal Registration Tracker (Handles Multi-Year Rollover)
CREATE TABLE IF NOT EXISTS annual_registrations (
    registration_id SERIAL PRIMARY KEY,
    player_id INT REFERENCES players(player_id) ON DELETE CASCADE,
    team_id INT REFERENCES teams(team_id) ON DELETE RESTRICT,
    season_year INT NOT NULL, -- e.g., 2026, 2027
    preferred_position VARCHAR(30), -- Goalkeeper, Pivot, Wing, Back
    dominant_hand VARCHAR(10), -- Left, Right
    medical_waiver_signed BOOLEAN DEFAULT FALSE,
    player_fee_status VARCHAR(20) DEFAULT 'PENDING', -- PENDING, PAID
    is_active_roster BOOLEAN DEFAULT TRUE,
    CONSTRAINT unique_player_season UNIQUE (player_id, season_year)
);

-- 4. Transfer Audit Ledger
CREATE TABLE IF NOT EXISTS transfer_history (
    transfer_id SERIAL PRIMARY KEY,
    player_id INT REFERENCES players(player_id),
    from_team_id INT REFERENCES teams(team_id),
    to_team_id INT REFERENCES teams(team_id),
    window_type VARCHAR(20) NOT NULL, -- MID_SEASON, END_OF_SEASON
    fee_paid_status VARCHAR(20) DEFAULT 'PENDING',
    approved_by_admin BOOLEAN DEFAULT FALSE,
    transfer_timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 5. Financial Audit Logs
CREATE TABLE IF NOT EXISTS financial_ledger (
    transaction_id SERIAL PRIMARY KEY,
    reference_number VARCHAR(100) UNIQUE NOT NULL, -- System generated invoice ref
    momo_mpamba_reference VARCHAR(100), -- External telecom gateway reference
    amount DECIMAL(10, 2) NOT NULL, -- e.g., 150.00 or 50.00
    payment_type VARCHAR(30) NOT NULL, -- TEAM_AFFILIATION, PLAYER_AFFILIATION, TRANSFER_FEE
    payment_status VARCHAR(20) DEFAULT 'PENDING', -- PENDING, SUCCESSFUL, FAILED
    payer_phone_number VARCHAR(30) NOT NULL,
    related_team_id INT REFERENCES teams(team_id),
    related_player_id INT REFERENCES players(player_id),
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 6. Seasons configuration table (for transfer windows)
CREATE TABLE IF NOT EXISTS seasons (
    season_id SERIAL PRIMARY KEY,
    season_year INT NOT NULL UNIQUE,
    start_date DATE NOT NULL,
    end_date DATE NOT NULL,
    mid_season_transfer_start DATE,
    mid_season_transfer_end DATE,
    end_season_transfer_start DATE,
    end_season_transfer_end DATE,
    is_active BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Useful indexes
CREATE INDEX IF NOT EXISTS idx_annual_reg_player ON annual_registrations(player_id);
CREATE INDEX IF NOT EXISTS idx_annual_reg_team ON annual_registrations(team_id);
CREATE INDEX IF NOT EXISTS idx_annual_reg_season ON annual_registrations(season_year);
CREATE INDEX IF NOT EXISTS idx_transfer_player ON transfer_history(player_id);
CREATE INDEX IF NOT EXISTS idx_financial_ref ON financial_ledger(reference_number);
CREATE INDEX IF NOT EXISTS idx_financial_status ON financial_ledger(payment_status);

-- Sample seed data for development (optional)
-- INSERT INTO seasons (season_year, start_date, end_date, is_active) 
-- VALUES (2026, '2026-03-01', '2026-11-30', TRUE);
