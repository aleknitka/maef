import os
import tempfile

# Import-time ledger creation must not touch the real runs/ directory.
os.environ.setdefault("MAEF_DB", os.path.join(tempfile.mkdtemp(prefix="maef-test-"), "maef.db"))
