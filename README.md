# ASKAP–MWA FRB Rapid-Response Triggering Project  
**Project Summary & Implementation Plan**

---

## 1. Project Overview

### 1.1 Background
Fast Radio Bursts (FRBs) detected by ASKAP can benefit significantly from rapid, low-frequency follow-up observations with the Murchison Widefield Array (MWA). With the recent commissioning of the CRACO coherent pipeline, ASKAP is now capable of producing low-latency, well-localised FRB candidates suitable for triggering rapid-response observations.

This project aims to establish a reliable, low-latency triggering pathway from ASKAP/CRACO FRB candidates to MWA observations.

---

### 1.2 Project Goal

**Primary objective (MVP):**  
To implement a stable, end-to-end pipeline that triggers MWA observations in response to FRB candidates identified by the CRACO system.

**Secondary / long-term objective:**  
To enable dissemination of validated ASKAP FRB triggers to the wider astronomical community (e.g. via GCN).

---

## 2. Scope and Non-Scope

### 2.1 In Scope
- Integration with the existing, operational CRACO system  
- Hooking into the CRACO **classification stage**  
- Triggering MWA via the existing **HTTP-based rapid-response interface**  
- MVP testing and validation using real or test triggers  
- Logging, monitoring, and basic robustness checks  

### 2.2 Out of Scope (for MVP)
- Modifications to the CRACO detection or classification algorithms  
- Security redesign of the MWA triggering API (HTTP / plaintext keys)  
- Full production-grade alert distribution infrastructure  
- Guaranteed continuous observing time on MWA

## 3. High-Level System Architecture
![ASKAP–CRACO–MWA trigger flow](fig/architecture.png)

**Key principle:**  
The project builds on top of existing CRACO operations without disrupting the running pipeline.

---





## 4. Triggering Logic

### 4.1 Trigger Signal
- Trigger condition:  
  **CRACO classification identifies an unknown source / FRB candidate**
- Trigger is **event-based**, not observation-state-based

### 4.2 Trigger Action
Upon trigger:
1. Extract relevant candidate metadata (time, position, confidence, etc.)
2. Package metadata into MWA-compatible trigger parameters
3. Send HTTP POST request to the MWA triggering endpoint  
   - Authentication via plaintext key (current MWA mechanism)
4. Log trigger outcome and metadata for traceability

---

## 5. CRACO Integration

### 5.1 Required Access
- Access to CRACO compute environment
- Read access to running pipeline outputs and logs

### 5.2 Classification Hook
- Identify existing CRACO script responsible for post-classification actions
- Insert trigger call at appropriate decision point
- Ensure minimal latency and no interference with existing operations

---

## 6. MWA Integration

### 6.1 Trigger Mechanism
- HTTP-based MWA triggering endpoint
- Parameters passed via URL query
- Plaintext key authentication (existing mechanism)

### 6.2 Observation Parameters
To be defined jointly with:
- Science leads (e.g. Clancy, Kat)
- MWA technical support (Andrew Williams)

Includes:
- Frequency setup
- Integration time
- VCS configuration

---

## 7. MVP Definition

### 7.1 MVP Success Criteria
- End-to-end trigger from CRACO classification to MWA execution
- Demonstrated low latency and stability
- Successful test observations (with or without FRB detection)
- Clear logging and traceability of trigger events

### 7.2 MVP Limitations
- Limited trigger rate
- Testing-focused usage
- No external alert distribution

---

## 8. Testing and Validation Plan

- Offline testing with simulated candidates
- On-sky testing via MWA rapid-response capability
- Measurement of:
  - Trigger latency
  - False trigger rate
  - System robustness
- Iterative refinement before production use

---

## 9. MWA Observing Strategy and Proposals

- Initial testing may proceed **without a formal MWA proposal**
- Formal proposal to be submitted once:
  - System stability is demonstrated
  - Event rates are characterised
- Ongoing communication with MWA team to manage scheduling constraints

---

## 10. Future Extensions (Post-MVP)

- Integration with alert dissemination systems (e.g. GCN)
- Standardised FRB alert schema
- Multi-observatory triggering support
- Improved security and authentication mechanisms

---

## 11. Roles and Responsibilities

| Role | Responsibility |
|-----|----------------|
| CRACO team | Candidate generation & classification |
| Trigger developer | Integration & MWA triggering logic |
| MWA support | Observation configuration & execution |
| Science leads | Scientific validation & strategy |

---

## 12. Risks and Mitigations

| Risk | Mitigation |
|------|------------|
| Delayed system access | Early coordination, backup contacts |
| Scheduling conflicts | Ongoing communication with MWA |
| False triggers | Conservative thresholds during MVP |
| Latency issues | Minimal processing in trigger hook |

---

## 13. Current Status & Next Steps

### Current Status
- Project scope aligned
- MVP definition agreed
- Key stakeholders identified

### Immediate Next Steps
1. Gain CRACO system access  
2. Identify classification trigger hook  
3. Define initial MWA observation template  
4. Implement and test MVP trigger path  

---


---

## 3. High-Level System Architecture

