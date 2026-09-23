"""数据库连接与会话管理。"""
from sqlalchemy import create_engine, event
from sqlalchemy.orm import declarative_base, sessionmaker

from .config import DATABASE_URL

# SQLite 需要关闭同线程检查以配合 FastAPI 的依赖注入；
# 开启 WAL 以支持读写并发，忙时等待而不是立即报锁冲突。
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False, "timeout": 30},
)


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_connection, _connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=30000")
    cursor.close()
    # 关闭 pysqlite 驱动的隐式事务，交由 SQLAlchemy 在 begin 事件中显式发起
    dbapi_connection.isolation_level = None


@event.listens_for(engine, "begin")
def _sqlite_begin_immediate(conn):
    # 每个事务都以 BEGIN IMMEDIATE 开启：立即获取写锁，
    # 让所有写事务在全库级别串行执行，配合条件 UPDATE 杜绝超卖/重复占位。
    conn.exec_driver_sql("BEGIN IMMEDIATE")


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    """FastAPI 依赖：提供一个数据库会话，请求结束后关闭。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
