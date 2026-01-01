/**
 * Lab Controller - Dashboard JavaScript
 */

// API helper functions
const api = {
    async get(url) {
        const response = await fetch(url);
        return response.json();
    },

    async post(url, data) {
        const response = await fetch(url, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify(data),
        });
        return response.json();
    },

    async put(url, data) {
        const response = await fetch(url, {
            method: 'PUT',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify(data),
        });
        return response.json();
    },

    async delete(url) {
        const response = await fetch(url, {
            method: 'DELETE',
        });
        return response.json();
    },
};

// Power control functions
async function powerOn(sbcName) {
    return api.post(`/api/sbcs/${sbcName}/power`, { action: 'on' });
}

async function powerOff(sbcName) {
    return api.post(`/api/sbcs/${sbcName}/power`, { action: 'off' });
}

async function powerCycle(sbcName) {
    return api.post(`/api/sbcs/${sbcName}/power`, { action: 'cycle' });
}

async function getPowerStatus(sbcName) {
    return api.get(`/api/sbcs/${sbcName}/power`);
}

// Status refresh
async function refreshStatus() {
    try {
        const data = await api.get('/api/status');
        console.log('Status updated:', data);
        // Could update DOM here without page refresh
    } catch (error) {
        console.error('Failed to refresh status:', error);
    }
}

// Auto-refresh status every 30 seconds (disabled by default)
// setInterval(refreshStatus, 30000);

// Initialize on page load
document.addEventListener('DOMContentLoaded', () => {
    console.log('Lab Controller dashboard loaded');
});
