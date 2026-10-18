import time
import sys
from datetime import datetime

class DummyAccount:
    def __init__(self, id, email):
        self.id = id
        self.email = email
        self.at = True
        self.at_expires = None
        self.last_used_at = None

accounts = [
    DummyAccount(1, "acc1@example.com"),
    DummyAccount(2, "acc2@example.com"),
    DummyAccount(3, "acc3@example.com"),
    DummyAccount(4, "acc4@example.com"),
    DummyAccount(5, "acc5@example.com"),
    DummyAccount(6, "acc6@example.com"),
]

def print_shift_for_time(simulated_time):
    shift_duration = 8 * 3600
    current_shift = int(simulated_time // shift_duration)
    accounts_per_shift = 2

    active_pool = []
    if len(accounts) <= accounts_per_shift:
        active_pool = accounts
    else:
        start_idx = (current_shift * accounts_per_shift) % len(accounts)
        for i in range(accounts_per_shift):
            idx = (start_idx + i) % len(accounts)
            active_pool.append(accounts[idx])
            
    dt = datetime.fromtimestamp(simulated_time).strftime('%Y-%m-%d %H:%M:%S')
    emails = [a.email for a in active_pool]
    print(f"Time: {dt} (Shift {current_shift}) -> Active: {emails}")

# Test normal rotation over 48 hours (6 shifts)
print("--- Normal 6 accounts rotation (48 hours) ---")
base_time = 1700000000 # arbitrary timestamp
for hour in range(0, 48 + 1, 8):
    print_shift_for_time(base_time + hour * 3600)

print("\n--- Fault Tolerance Test (Account 3 dies at hour 16) ---")
for hour in range(0, 48 + 1, 8):
    if hour == 16:
        print(">> Account 3 gets banned/disabled <<")
        accounts = [a for a in accounts if a.id != 3]
    print_shift_for_time(base_time + hour * 3600)

print("\n--- Fault Tolerance Test (N = 5 accounts) ---")
accounts = [
    DummyAccount(1, "acc1@example.com"),
    DummyAccount(2, "acc2@example.com"),
    DummyAccount(3, "acc3@example.com"),
    DummyAccount(4, "acc4@example.com"),
    DummyAccount(5, "acc5@example.com"),
]
for hour in range(0, 48 + 1, 8):
    print_shift_for_time(base_time + hour * 3600)
    
print("\n--- Fault Tolerance Test (N = 1 account) ---")
accounts = [
    DummyAccount(1, "acc1@example.com"),
]
for hour in range(0, 48 + 1, 8):
    print_shift_for_time(base_time + hour * 3600)
