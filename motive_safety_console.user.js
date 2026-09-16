// ==UserScript==
// @name         Motive Fleet Safety Console
// @namespace    http://tampermonkey.net/
// @version      1.1
// @description  Automated dashcam monitor for Seatbelt and Phone compliance on Motive Fleet
// @author       Fleet Safety Automation
// @match        *://*.gomotive.com/*
// @match        https://app.gomotive.com/*
// @run-at       document-idle
// @grant        none
// ==/UserScript==

(function() {
    'use strict';

    console.log("[Safety Console] Script initialized on:", window.location.href);

    function initButton() {
        if (document.getElementById("safetyConsoleTrigger")) return;
        if (!document.body) return;

        console.log("[Safety Console] Injecting Safety Console button...");

        // Floating trigger button
        const triggerBtn = document.createElement('button');
        triggerBtn.id = "safetyConsoleTrigger";
        triggerBtn.innerHTML = "🛡️ Safety Console";
        triggerBtn.style.cssText = `
            position: fixed !important;
            bottom: 24px !important;
            right: 24px !important;
            z-index: 2147483647 !important;
            background: #f59e0b !important;
            color: #0f172a !important;
            font-weight: 700 !important;
            font-size: 13px !important;
            padding: 10px 18px !important;
            border-radius: 9999px !important;
            border: 2px solid #ffffff !important;
            box-shadow: 0 10px 25px -5px rgba(0, 0, 0, 0.5) !important;
            cursor: pointer !important;
            font-family: system-ui, -apple-system, sans-serif !important;
            transition: transform 0.2s, background 0.2s !important;
        `;
        triggerBtn.onmouseenter = () => triggerBtn.style.transform = "scale(1.06)";
        triggerBtn.onmouseleave = () => triggerBtn.style.transform = "scale(1)";
        document.body.appendChild(triggerBtn);

        // Modal container
        let modal = document.getElementById("safetyConsoleModal");
        if (!modal) {
            modal = document.createElement('div');
            modal.id = "safetyConsoleModal";
            modal.style.cssText = `
                display: none;
                position: fixed;
                top: 0;
                left: 0;
                width: 100vw;
                height: 100vh;
                background: rgba(15, 23, 42, 0.85);
                backdrop-filter: blur(6px);
                z-index: 2147483647;
                align-items: center;
                justify-content: center;
                padding: 24px;
                box-sizing: border-box;
                font-family: system-ui, -apple-system, sans-serif;
            `;

            modal.innerHTML = `
                <div style="background: #1e293b; border: 1px solid #334155; width: 100%; max-width: 1200px; height: 90vh; border-radius: 16px; display: flex; flex-direction: column; overflow: hidden; box-shadow: 0 25px 50px -12px rgba(0,0,0,0.5);">
                    <div style="background: #0f172a; padding: 14px 20px; border-bottom: 1px solid #334155; display: flex; justify-content: space-between; align-items: center;">
                        <div style="display: flex; align-items: center; gap: 10px;">
                            <div style="background: #f59e0b; color: #0f172a; padding: 6px 10px; border-radius: 8px; font-weight: bold;">🛡️</div>
                            <div>
                                <h2 style="margin: 0; font-size: 16px; font-weight: 700; color: #ffffff;">Motive In-Browser Safety Console</h2>
                                <p style="margin: 0; font-size: 11px; color: #94a3b8;">Filter: HEART & unassigned &bull; In transit & idling</p>
                            </div>
                        </div>
                        <div style="display: flex; gap: 8px; align-items: center;">
                            <button id="scScanFleetBtn" style="background: #334155; color: #fff; border: 1px solid #475569; padding: 6px 12px; border-radius: 8px; font-size: 12px; cursor: pointer; font-weight: 600;">Scan Page Vehicles</button>
                            <button id="scExportCsvBtn" style="background: #10b981; color: #fff; border: none; padding: 6px 12px; border-radius: 8px; font-size: 12px; cursor: pointer; font-weight: 600;">Export CSV</button>
                            <button id="scCloseBtn" style="background: transparent; border: none; color: #94a3b8; font-size: 24px; cursor: pointer; padding: 2px 8px; line-height: 1;">&times;</button>
                        </div>
                    </div>

                    <div style="padding: 12px 20px; background: #1e293b; border-bottom: 1px solid #334155; display: flex; justify-content: space-between; font-size: 12px; color: #94a3b8;">
                        <span id="scStatusText">Ready. Go to Fleet View > Vehicles, then click "Scan Page Vehicles".</span>
                        <span id="scCountText">0 Target Vehicles</span>
                    </div>

                    <div id="scGrid" style="flex: 1; overflow-y: auto; padding: 20px; display: grid; grid-template-columns: repeat(auto-fill, minmax(480px, 1fr)); gap: 16px; background: #0f172a;">
                        <div style="grid-column: 1 / -1; text-align: center; padding: 60px 0; color: #64748b;">
                            <p style="font-size: 14px; margin-bottom: 6px; font-weight: 600;">No vehicles scanned yet.</p>
                            <p style="font-size: 12px;">Make sure you are on <strong>Fleet View &gt; Vehicles</strong> and click <strong>"Scan Page Vehicles"</strong> above.</p>
                        </div>
                    </div>
                </div>
            `;
            document.body.appendChild(modal);

            triggerBtn.onclick = () => {
                modal.style.display = 'flex';
                scanFleet();
            };
            document.getElementById('scCloseBtn').onclick = () => modal.style.display = 'none';
            document.getElementById('scScanFleetBtn').onclick = scanFleet;
            document.getElementById('scExportCsvBtn').onclick = exportCsv;
        }
    }

    let violationLogs = [];

    function scanFleet() {
        const rows = document.querySelectorAll('table tbody tr, [role="row"], .vehicle-row');
        const grid = document.getElementById('scGrid');
        const status = document.getElementById('scStatusText');
        const countBadge = document.getElementById('scCountText');

        if (!rows.length) {
            status.innerText = "No vehicle table found on this page. Please navigate to Fleet View > Vehicles first.";
            return;
        }

        let targets = [];
        rows.forEach(r => {
            const text = r.innerText || "";
            const lower = text.toLowerCase();
            const hasStatus = ["in transit", "moving", "idling", "idle"].some(s => lower.includes(s));
            if (!hasStatus) return;

            const isHeart = lower.includes("heart");
            const lines = text.split("\n").map(l => l.trim()).filter(Boolean);
            const vName = lines[0] || "Vehicle";
            const driver = lines.find(l => !l.includes("mph") && !l.includes("transit") && !l.includes("idling") && l !== vName) || "Unassigned";

            targets.push({
                element: r,
                name: vName,
                driver: driver,
                group: isHeart ? "HEART" : "Unassigned",
                status: lower.includes("transit") || lower.includes("moving") ? "In Transit" : "Idling"
            });
        });

        countBadge.innerText = `${targets.length} Target Vehicles`;
        status.innerText = `Found ${targets.length} matching vehicles (HEART & In Transit / Idling).`;

        if (!targets.length) {
            grid.innerHTML = `<div style="grid-column: 1 / -1; text-align: center; padding: 40px; color: #94a3b8;">No vehicles currently In Transit or Idling.</div>`;
            return;
        }

        grid.innerHTML = targets.map((t, idx) => `
            <div style="background: #1e293b; border: 1px solid #334155; border-radius: 12px; padding: 14px; display: flex; flex-direction: column; gap: 10px;">
                <div style="display: flex; justify-content: space-between; align-items: center;">
                    <div>
                        <strong style="color: #ffffff; font-size: 14px;">${t.name}</strong>
                        <span style="font-size: 10px; background: ${t.group === 'HEART' ? '#e11d48' : '#475569'}; color: #fff; padding: 2px 6px; border-radius: 4px; margin-left: 6px; font-weight: bold;">${t.group}</span>
                        <div style="font-size: 11px; color: #94a3b8; margin-top: 2px;">Driver: <span style="color: #cbd5e1;">${t.driver}</span></div>
                    </div>
                    <span style="font-size: 11px; background: #065f46; color: #34d399; padding: 3px 8px; border-radius: 6px; font-weight: 600;">${t.status}</span>
                </div>

                <button onclick="window.scInspectVehicle(${idx})" style="background: #f59e0b; color: #0f172a; border: none; padding: 8px; border-radius: 6px; font-weight: bold; font-size: 11px; cursor: pointer;">
                    🎥 Open Vehicle Profile in Motive
                </button>

                <div style="display: flex; gap: 6px; flex-wrap: wrap; margin-top: 4px;">
                    <button id="sb-tag-${idx}" onclick="window.scToggleViolation('sb', ${idx})" style="flex: 1; background: #334155; color: #e2e8f0; border: 1px solid #475569; padding: 6px; border-radius: 6px; font-size: 10px; font-weight: 600; cursor: pointer;">
                        Seatbelt: OK
                    </button>
                    <button id="phone-tag-${idx}" onclick="window.scToggleViolation('phone', ${idx})" style="flex: 1; background: #334155; color: #e2e8f0; border: 1px solid #475569; padding: 6px; border-radius: 6px; font-size: 10px; font-weight: 600; cursor: pointer;">
                        Phone: Clear
                    </button>
                    <button onclick="window.scLogCheck(${idx}, '${t.name}', '${t.driver}')" style="background: #0284c7; color: #fff; border: none; padding: 6px 12px; border-radius: 6px; font-size: 10px; font-weight: bold; cursor: pointer;">
                        Log Check
                    </button>
                </div>
            </div>
        `).join('');

        window.scTargets = targets;
    }

    window.scInspectVehicle = function(idx) {
        const target = window.scTargets[idx];
        if (target && target.element) {
            target.element.click();
            document.getElementById('safetyConsoleModal').style.display = 'none';
        }
    };

    window.scToggleViolation = function(type, idx) {
        const btn = document.getElementById(`${type}-tag-${idx}`);
        if (type === 'sb') {
            if (btn.innerText.includes("OK")) {
                btn.innerText = "⚠️ NO SEATBELT";
                btn.style.background = "#dc2626";
                btn.style.color = "#fff";
                btn.setAttribute("data-violation", "true");
            } else {
                btn.innerText = "Seatbelt: OK";
                btn.style.background = "#334155";
                btn.style.color = "#e2e8f0";
                btn.removeAttribute("data-violation");
            }
        } else if (type === 'phone') {
            if (btn.innerText.includes("Clear")) {
                btn.innerText = "📱 PHONE IN HAND";
                btn.style.background = "#dc2626";
                btn.style.color = "#fff";
                btn.setAttribute("data-violation", "true");
            } else {
                btn.innerText = "Phone: Clear";
                btn.style.background = "#334155";
                btn.style.color = "#e2e8f0";
                btn.removeAttribute("data-violation");
            }
        }
    };

    window.scLogCheck = function(idx, vName, driver) {
        const sbViol = document.getElementById(`sb-tag-${idx}`).getAttribute("data-violation") === "true";
        const phoneViol = document.getElementById(`phone-tag-${idx}`).getAttribute("data-violation") === "true";

        violationLogs.push({
            timestamp: new Date().toLocaleString(),
            vehicle: vName,
            driver: driver,
            seatbelt: sbViol ? "VIOLATION" : "OK",
            phone: phoneViol ? "VIOLATION" : "Clear"
        });

        alert(`Logged safety check for ${vName}:\nSeatbelt: ${sbViol ? "NO SEATBELT" : "OK"}\nPhone: ${phoneViol ? "PHONE IN HAND" : "Clear"}`);
    };

    function exportCsv() {
        if (!violationLogs.length) {
            alert("No logs recorded yet!");
            return;
        }
        let csv = "Timestamp,Vehicle,Driver,Seatbelt,Phone\n";
        violationLogs.forEach(r => {
            csv += `"${r.timestamp}","${r.vehicle}","${r.driver}","${r.seatbelt}","${r.phone}"\n`;
        });
        const blob = new Blob([csv], { type: 'text/csv' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `motive_safety_checks_${new Date().toISOString().slice(0,10)}.csv`;
        a.click();
    }

    // Try injecting immediately and keep checking every 1.5s in case of SPA re-renders
    initButton();
    setInterval(initButton, 1500);

})();
