import tkinter as tk
from tkinter import ttk, messagebox
import requests
from datetime import datetime
import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
from matplotlib.figure import Figure
import matplotlib.ticker as ticker

# --- CONFIGURATION ---
NORDPOOL_USER = "API_DATA_MEHDI"
NORDPOOL_PASSWORD = "OsloNordpool@123"
TOKEN_URL = "https://sts.nordpoolgroup.com/connect/token"
PROD_URL = "https://data-api.nordpoolgroup.com/api/v2/PowerSystem/Productions/ByLocations"
CONS_URL = "https://data-api.nordpoolgroup.com/api/v2/PowerSystem/Consumptions/ByLocations"

def get_np_access_token():
    headers = {
        'Authorization': 'Basic Y2xpZW50X21hcmtldGRhdGFfYXBpOmNsaWVudF9tYXJrZXRkYXRhX2FwaQ==',
        'Content-Type': 'application/x-www-form-urlencoded'
    }
    data = {'grant_type': 'password', 'scope': 'marketdata_api', 'username': NORDPOOL_USER, 'password': NORDPOOL_PASSWORD}
    try:
        response = requests.post(TOKEN_URL, headers=headers, data=data, timeout=10)
        response.raise_for_status()
        return response.json().get("access_token")
    except Exception as e:
        print(f"Auth Error: {e}")
        return None

class IntegratedTester:
    def __init__(self, root):
        self.root = root
        self.root.title("Nord Pool Gen & Cons Tester")
        self.root.geometry("1100x800")

        # Control Panel
        ctrl = ttk.Frame(root, padding=10); ctrl.pack(fill=tk.X)
        
        ttk.Label(ctrl, text="Zone:").pack(side=tk.LEFT)
        self.zone_entry = ttk.Entry(ctrl, width=10); self.zone_entry.insert(0, "NO"); self.zone_entry.pack(side=tk.LEFT, padx=5)
        
        ttk.Label(ctrl, text="Type:").pack(side=tk.LEFT)
        self.type_var = tk.StringVar(value="windOnshore")
        # Added 'Consumption' to the list
        self.combo = ttk.Combobox(ctrl, textvariable=self.type_var, 
                                  values=["windOnshore", "hydroWaterReservoir", "nuclear", "total", "Consumption"])
        self.combo.pack(side=tk.LEFT, padx=5)

        ttk.Label(ctrl, text="Date (YYYY-MM-DD):").pack(side=tk.LEFT)
        self.date_entry = ttk.Entry(ctrl, width=12); self.date_entry.insert(0, "2025-05-05"); self.date_entry.pack(side=tk.LEFT, padx=5)

        ttk.Button(ctrl, text="Fetch & Plot", command=self.plot_data).pack(side=tk.LEFT, padx=10)

        # Plot Area
        self.fig = Figure(figsize=(10, 6), dpi=100)
        self.canvas = FigureCanvasTkAgg(self.fig, root)
        self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)

    def plot_data(self):
        self.fig.clear()
        zone, dtype, target_date = self.zone_entry.get(), self.type_var.get(), self.date_entry.get()
        
        token = get_np_access_token()
        if not token: return

        print(f"Fetching {dtype} for {zone}...")
        headers = {'Authorization': f'Bearer {token}', 'accept': 'application/json'}
        
        # Select URL based on type
        url = CONS_URL if dtype == "Consumption" else PROD_URL
        
        try:
            r = requests.get(url, headers=headers, params={'date': target_date, 'locations': zone}, timeout=10)
            if r.status_code == 200:
                raw_data = r.json()
                if not raw_data: 
                    print("Empty response"); return
                
                all_values = []
                all_labels = []
                location_obj = raw_data[0]

                # --- BRANCH LOGIC FOR CONS VS PROD ---
                if dtype == "Consumption":
                    data_list = location_obj.get('consumptions', [])
                    for entry in data_list:
                        ts = entry.get('deliveryStart')
                        lbl = datetime.fromisoformat(ts.replace('Z', '+00:00')).strftime('%H:%M')
                        val = entry.get('volume', 0) # Key for Consumption is 'volume'
                        all_values.append(val)
                        all_labels.append(lbl)
                else:
                    data_list = location_obj.get('productions', [])
                    for entry in data_list:
                        ts = entry.get('deliveryStart')
                        lbl = datetime.fromisoformat(ts.replace('Z', '+00:00')).strftime('%H:%M')
                        if dtype == "total":
                            val = entry.get('total', 0)
                        else:
                            val = entry.get('byType', {}).get(dtype, 0)
                        all_values.append(val)
                        all_labels.append(lbl)

                print(f"Parsed {len(all_values)} points.")

                # Plotting
                ax = self.fig.add_subplot(111)
                ax.bar(all_labels, all_values, color='#dbeafe', width=0.8, label=dtype)
                ax.set_ylabel("MW")
                ax.set_title(f"Nord Pool {dtype} in {zone} ({target_date})", fontweight='bold')
                
                # Show every 8th label (every 2 hours in 15-min data)
                ax.xaxis.set_major_locator(ticker.MaxNLocator(nbins=12))
                ax.grid(True, axis='y', linestyle=':', alpha=0.5)
                
                for label in ax.get_xticklabels():
                    label.set_rotation(45)
                    label.set_fontsize(8)

                self.fig.tight_layout()
                self.canvas.draw()
            else:
                print(f"Error: {r.status_code}")
        except Exception as e:
            print(f"Exception: {e}")

if __name__ == "__main__":
    root = tk.Tk(); app = IntegratedTester(root); root.mainloop()