import os
import tempfile

# 在导入 app 之前把数据库指向临时目录，避免测试污染仓库目录
os.environ.setdefault("DB_PATH", os.path.join(tempfile.mkdtemp(prefix="traindb-"), "test.db"))
os.environ.setdefault("SAMPLE_TIMETABLE", "")
