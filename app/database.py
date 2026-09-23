"""数据库连接与会话管理。

SQLite 写事务统一以 BEGIN IMMEDIATE 开启：进入事务立即获取写锁，
将并发写请求在数据库层串行化，保证预约占位、换电扣减等计数操作
在多线程/多请求并发下仍然确定、不超卖。
"""
from sqlalchemy import create_engine, event
from sqlalchemy.orm import declarative_base, sessionmaker

from .config import DATABASE_URL

# SQLite 需要关闭同线程检查以配合 FastAPI 的依赖注入
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False, "timeout": 30},
)


@event.listens_for(engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record):
    """开启外键约束；写事务在首个写操作时立即升级为保留锁。"""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()
    # SQLAlchemy 对 SELECT 不会自动开事务，首个 INSERT/UPDATE 会执行
    # BEGIN IMMEDIATE 而非惰性的 deferred 事务，先到先得、后来者等待/报错。
    dbapi_connection.isolation_level = None


@event.listens_for(engine, "begin")
def _begin_immediate(connection):
    """每个新事务都以 BEGIN IMMEDIATE 开始，串行化所有写事务。"""
    connection.exec_driver_sql("BEGIN IMMEDIATE")


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    """FastAPI 依赖：提供一个数据库会话，请求结束后关闭。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
