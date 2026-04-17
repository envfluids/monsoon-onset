import os

days = range(8, 17)

for day in days:
    cmd = f"python plot.py --date 202604{str(day).zfill(2)}"
    print(f"Running command: {cmd}")
    os.system(cmd)
    cmd = f"python plot_bias.py --date 202604{str(day).zfill(2)}"
    print(f"Running command: {cmd}")
    os.system(cmd)
