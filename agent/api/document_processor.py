import os
import shutil
import logging
from typing import List

from langchain_community.document_loaders import DirectoryLoader, TextLoader
from langchain_community.vectorstores import FAISS
from langchain_core.documents import Document
from langchain_core.vectorstores import VectorStoreRetriever
from langchain_text_splitters import MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter

from .config import settings
from .llm_setup import embedding_function


_LOGGER = logging.getLogger(__name__)


class MarkdownDocumentProcessor:
    """Класс для обработки .md."""

    def __init__(
        self,
        chunk_size: int = 1024,
        chunk_overlap: int = 256,
        include_headers_in_content: bool = True,
    ):
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.include_headers_in_content = include_headers_in_content

        self.headers_to_split_on = [
            ("#", "Header 1"),
            ("##", "Header 2"),
            ("###", "Header 3"),
            ("####", "Header 4"),
        ]

    def enrich_metadata(self, chunks: List[Document]) -> List[Document]:
        enriched = []
        for chunk in chunks:
            md = dict(chunk.metadata)
            src = md.get("source", "")
            md["filename"] = src.split("/")[-1]
            md["preview"] = chunk.page_content[:50] + "..."
            enriched.append(Document(page_content=chunk.page_content, metadata=md))
        return enriched

    def process_documents(self, documents: List[Document]) -> List[Document]:
        final_chunks = []

        markdown_splitter = MarkdownHeaderTextSplitter(headers_to_split_on=self.headers_to_split_on)
        text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=self.chunk_size,
            chunk_overlap=self.chunk_overlap,
        )

        for doc in documents:
            structurally_split_chunks = markdown_splitter.split_text(doc.page_content)

            for chunk in structurally_split_chunks:
                # Объединяем метаданные из исходного документа с метаданными заголовков
                chunk.metadata = {**doc.metadata, **chunk.metadata}

                if self.include_headers_in_content:
                    header_text = ""
                    for i in range(1, 5):
                        header_key = f"Header {i}"
                        if header_key in chunk.metadata:
                            header_text += f"{'#' * i} {chunk.metadata[header_key]}\n\n"
                    chunk.page_content = f"{header_text}{chunk.page_content}"

            further_split_chunks = text_splitter.split_documents(structurally_split_chunks)
            final_chunks.extend(further_split_chunks)

        return self.enrich_metadata(final_chunks)


def initialize_vector_db_for_path(path: str):
    if not os.path.exists(path):
        os.makedirs(path, exist_ok=True)
        _LOGGER.info(f"--- dir {path} for vector db ---")
    documents = load_documents(settings.docs_path if path == settings.chroma_path else path)
    if not documents:
        _LOGGER.warning(f"--- Нет документов в {path}. Создаю пустой индекс ---")
        # Чтобы не падать — создаём минимальный индекс
        dummy = [Document(page_content="__EMPTY__", metadata={"filename": "__EMPTY__"})]
        return create_and_persist_faiss(dummy, embedding_function)

    processor = MarkdownDocumentProcessor(
        chunk_size=settings.chunk_size,
        chunk_overlap=settings.chunk_overlap,
        include_headers_in_content=True,
    )
    ready_chunks = processor.process_documents(documents)
    db = create_and_persist_faiss(ready_chunks, embedding_function)
    return db


# фабрика ретриверов с MMR/параметрами
def make_retriever(db, *, search_type="mmr", k=None, fetch_k=None, lambda_mult=None) -> VectorStoreRetriever:
    skw = {"k": k or settings.num_of_base_vectors}
    if fetch_k is not None:
        skw["fetch_k"] = fetch_k
    if lambda_mult is not None:
        skw["lambda_mult"] = lambda_mult
    return db.as_retriever(search_type=search_type, search_kwargs=skw)


def initialize_vector_db():
    """
    Полный цикл: загрузка документов, обработка и создание/загрузка векторной БД.
    Возвращает готовый к работе ретривер
    """
    try:
        db = initialize_vector_db_for_path(settings.chroma_path)
        _LOGGER.info("--- vb good! ---")
        return make_retriever(
            db,
            search_type=settings.retriever_main_search_type,
            k=settings.num_of_base_vectors,
            fetch_k=settings.retriever_main_fetch_k,
            lambda_mult=settings.retriever_main_lambda_mult,
        )
    except Exception as e:
        _LOGGER.error(F"[initialize_vector_db] Ошибка при инициализации векторной БД: {e}")
        raise


def load_documents(path: str) -> List[Document]:
    # читаем markdown как обычный текст (без unstructured и сетевых зависимостей)
    try:
        loader = DirectoryLoader(
            path,
            glob="**/*.md",
            loader_cls=TextLoader,
            loader_kwargs={"encoding": "utf-8", "autodetect_encoding": False},
            show_progress=True,
        )
        return loader.load()
    except Exception as e:
        _LOGGER.error(f"[load_documents] Ошибка при загрузке документов из '{path}': {e}")
        return []


def create_and_persist_faiss(chunks: List[Document], embedding_function) -> FAISS:
    if not chunks:
        _LOGGER.warning("--- There are not any documents for indexing ---")
        return FAISS.from_texts(["__EMPTY__"], embedding_function)
    # Полностью пересоздаём индекс при запуске (аналогично вашей логике с Chroma)
    if os.path.exists(settings.chroma_path):
        _LOGGER.info(f"--- Очистка старого индекса в '{settings.chroma_path}'... ---")
        shutil.rmtree(settings.chroma_path)
    os.makedirs(settings.chroma_path, exist_ok=True)

    _LOGGER.info("--- Создание эмбеддингов и сохранение в FAISS... ---")
    db = FAISS.from_documents(chunks, embedding_function)
    db.save_local(settings.chroma_path)  # создаст index.faiss и index.pkl
    _LOGGER.info(f"--- Индекс FAISS успешно сохранён в '{settings.chroma_path}' ---")
    return db
