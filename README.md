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

---

## 3. High-Level System Architecture

