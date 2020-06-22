import itertools
import logging
from warnings import warn

import flair
import ftfy
import json
import os
import shutil

from abc import ABC, abstractmethod
from collections import defaultdict, deque
from copy import copy
from operator import attrgetter
from pathlib import Path
from typing import Union, Callable, Dict, List, Tuple, Iterable
from lxml import etree
from lxml.etree import XMLSyntaxError

from flair.file_utils import cached_path, Tqdm, unpack_file
from flair.datasets import ColumnCorpus

DISEASE_TAG = "Disease"
CHEMICAL_TAG = "Chemical"
CELL_LINE_TAG = "CellLine"
GENE_TAG = "Gene"
SPECIES_TAG = "Species"

SENTENCE_TAG = "[__SENT__]"

logger = logging.getLogger("flair")


class Entity:
    def __init__(self, char_span: Tuple[int, int], entity_type: str):
        assert char_span[0] < char_span[1]
        self.char_span = range(*char_span)
        self.type = entity_type

    def __str__(self):
        return (
            self.type
            + "("
            + str(self.char_span.start)
            + ","
            + str(self.char_span.stop)
            + ")"
        )

    def __repr__(self):
        return str(self)

    def is_before(self, other_entity) -> bool:
        """
        Checks whether this entity is located before the given one

        :param other_entity: Entity to check
        """
        return self.char_span.stop <= other_entity.char_span.start

    def contains(self, other_entity) -> bool:
        """
        Checks whether the given entity is fully contained in this entity

        :param other_entity: Entity to check
        """
        return (
            other_entity.char_span.start >= self.char_span.start
            and other_entity.char_span.stop <= self.char_span.stop
        )

    def overlaps(self, other_entity) -> bool:
        """
        Checks whether this and the given entity overlap

        :param other_entity: Entity to check
        """
        return (
            self.char_span.start <= other_entity.char_span.start < self.char_span.stop
        ) or (self.char_span.start < other_entity.char_span.stop <= self.char_span.stop)


class InternalBioNerDataset:
    def __init__(
        self, documents: Dict[str, str], entities_per_document: Dict[str, List[Entity]]
    ):
        self.documents = documents
        self.entities_per_document = entities_per_document


def merge_datasets(data_sets: Iterable[InternalBioNerDataset]):
    all_documents = {}
    all_entities = {}

    for ds in data_sets:
        all_documents.update(ds.documents)
        all_entities.update(ds.entities_per_document)

    return InternalBioNerDataset(
        documents=all_documents, entities_per_document=all_entities
    )


def filter_and_map_entities(
    dataset: InternalBioNerDataset, entity_type_to_canonical: Dict[str, str]
) -> InternalBioNerDataset:
    """
    :param entity_type_to_canonical: Maps entity type in dataset to canonical type
                                     if entity type is not present in map it is discarded
    """
    mapped_entities_per_document = {}
    for id, entities in dataset.entities_per_document.items():
        new_entities = []
        for entity in entities:
            if entity.type in entity_type_to_canonical:
                new_entity = copy(entity)
                new_entity.type = entity_type_to_canonical[entity.type]
                new_entities.append(new_entity)
            else:
                logging.debug(f"Skip entity type {entity.type}")
                pass
        mapped_entities_per_document[id] = new_entities

    return InternalBioNerDataset(
        documents=dataset.documents, entities_per_document=mapped_entities_per_document
    )


def filter_nested_entities(dataset: InternalBioNerDataset) -> None:
    num_entities_before = sum([len(x) for x in dataset.entities_per_document.values()])

    for document_id, entities in dataset.entities_per_document.items():
        # Uses dynamic programming approach to calculate maximum independent set in interval graph
        # with sum of all entity lengths as secondary key
        dp_array = [
            (0, 0, 0, None)
        ]  # position_end, number of entities, sum of all entity lengths, last entity
        for entity in sorted(entities, key=lambda x: x.char_span.stop):
            i = len(dp_array) - 1
            while dp_array[i][0] > entity.char_span.start:
                i -= 1
            if dp_array[i][1] + 1 > dp_array[-1][1] or (
                dp_array[i][1] + 1 == dp_array[-1][1]
                and dp_array[i][2] + len(entity.char_span) > dp_array[-1][2]
            ):
                dp_array += [
                    (
                        entity.char_span.stop,
                        dp_array[i][1] + 1,
                        dp_array[i][2] + len(entity.char_span),
                        entity,
                    )
                ]
            else:
                dp_array += [dp_array[-1]]

        independent_set = []
        p = dp_array[-1][0]
        for dp_entry in dp_array[::-1]:
            if dp_entry[3] is None:
                break
            if dp_entry[0] <= p:
                independent_set += [dp_entry[3]]
                p -= len(dp_entry[3].char_span)

        dataset.entities_per_document[document_id] = independent_set

    num_entities_after = sum([len(x) for x in dataset.entities_per_document.values()])
    if num_entities_before != num_entities_after:
        removed = num_entities_before - num_entities_after
        warn(
            f"Corpus modified by filtering nested entities. Removed {removed} entities."
        )


def whitespace_tokenize(text: str) -> Tuple[List[str], List[int]]:
    offset = 0
    tokens = []
    offsets = []
    for token in text.split():
        tokens.append(token)
        offsets.append(offset)
        offset += len(token) + 1

    return tokens, offsets


def sentence_split_at_tag(text: str) -> Tuple[List[str], List[int]]:
    sentences = text.split(SENTENCE_TAG)
    offsets = []
    last_offset = 0
    for sent in sentences:
        offsets += [last_offset]
        last_offset += len(sent) + len(SENTENCE_TAG)

    return sentences, offsets


def sentence_split_at_newline(text: str) -> Tuple[List[str], List[int]]:
    sentences = text.split("\n")
    offsets = []
    last_offset = 0
    for sent in sentences:
        offsets += [last_offset]
        last_offset += len(sent) + 1

    return sentences, offsets


def sentence_split_one_sentence_per_doc(text: str) -> Tuple[List[str], List[int]]:
    return [text], [0]


def bioc_to_internal(bioc_file: Path):
    tree = etree.parse(str(bioc_file))
    texts_per_document = {}
    entities_per_document = {}
    documents = tree.xpath(".//document")

    all_entities = 0
    non_matching = 0

    for document in Tqdm.tqdm(documents, desc="Converting to internal"):
        document_id = document.xpath("./id")[0].text
        texts = []
        entities = []

        for passage in document.xpath("passage"):
            passage_texts = passage.xpath("text/text()")
            if len(passage_texts) == 0:
                continue

            text = passage_texts[0]
            passage_offset = int(
                passage.xpath("./offset/text()")[0]
            )  # from BioC annotation

            # calculate offset without current text
            # because we stick all passages of a document together
            document_text = " ".join(texts)
            document_offset = len(document_text)

            texts.append(text)
            document_text += " " + text

            for annotation in passage.xpath(".//annotation"):

                entity_types = [
                    i.text.replace(" ", "_")
                    for i in annotation.xpath("./infon")
                    if i.attrib["key"] in {"type", "class"}
                ]

                start = (
                    int(annotation.xpath("./location")[0].get("offset"))
                    - passage_offset
                )
                # TODO For split entities we also annotate everything inbetween which might be a bad idea?
                final_length = int(annotation.xpath("./location")[-1].get("length"))
                final_offset = (
                    int(annotation.xpath("./location")[-1].get("offset"))
                    - passage_offset
                )
                if final_length <= 0:
                    continue
                end = final_offset + final_length

                start += document_offset
                end += document_offset

                true_entity = annotation.xpath(".//text")[0].text
                annotated_entity = " ".join(texts)[start:end]

                # Try to fix incorrect annotations
                if annotated_entity.lower() != true_entity.lower():
                    max_shift = min(3, len(true_entity))
                    for i in range(max_shift):
                        index = annotated_entity.lower().find(
                            true_entity[0 : max_shift - i].lower()
                        )
                        if index != -1:
                            start += index
                            end += index
                            break

                annotated_entity = " ".join(texts)[start:end]
                if not annotated_entity.lower() == true_entity.lower():
                    non_matching += 1

                all_entities += 1

                for entity_type in entity_types:
                    entities.append(Entity((start, end), entity_type))

        texts_per_document[document_id] = " ".join(texts)
        entities_per_document[document_id] = entities

    # print(
    #     f"Found {non_matching} non-matching entities ({non_matching/all_entities}%) in {bioc_file}"
    # )

    return InternalBioNerDataset(
        documents=texts_per_document, entities_per_document=entities_per_document
    )


class CoNLLWriter:
    def __init__(
        self,
        tokenizer: Callable[[str], Tuple[List[str], List[int]]],
        sentence_splitter: Callable[[str], Tuple[List[str], List[int]]],
    ):
        """
        :param tokenizer: Callable that segments a sentence into words
        :param sentence_splitter: Callable that segments a document into sentences
        """
        self.tokenizer = tokenizer
        self.sentence_splitter = sentence_splitter

    def process_dataset(
        self, datasets: Dict[str, InternalBioNerDataset], out_dir: Path
    ):
        self.write_to_conll(datasets["train"], out_dir / "train.conll")
        self.write_to_conll(datasets["dev"], out_dir / "dev.conll")
        self.write_to_conll(datasets["test"], out_dir / "test.conll")

    def write_to_conll(self, dataset: InternalBioNerDataset, output_file: Path):
        os.makedirs(str(output_file.parent), exist_ok=True)
        filter_nested_entities(dataset)

        with output_file.open("w") as f:
            for document_id in Tqdm.tqdm(
                dataset.documents.keys(),
                total=len(dataset.documents),
                desc="Converting to CoNLL",
            ):
                document_text = ftfy.fix_text(dataset.documents[document_id])
                sentences, sentence_offsets = self.sentence_splitter(document_text)
                entities = deque(
                    sorted(
                        dataset.entities_per_document[document_id],
                        key=attrgetter("char_span.start", "char_span.stop"),
                    )
                )

                current_entity = entities.popleft() if entities else None

                for sentence, sentence_offset in zip(sentences, sentence_offsets):
                    in_entity = False
                    sentence_had_tokens = False
                    tokens, token_offsets = self.tokenizer(sentence)
                    for token, token_offset in zip(tokens, token_offsets):
                        token = token.strip()
                        offset = sentence_offset + token_offset

                        if current_entity and offset >= current_entity.char_span.stop:
                            in_entity = False

                            # One token may contain multiple entities -> deque all of them
                            while (
                                current_entity
                                and offset >= current_entity.char_span.stop
                            ):
                                current_entity = (
                                    entities.popleft() if entities else None
                                )

                        if current_entity and offset in current_entity.char_span:
                            if not in_entity:
                                tag = "B-" + current_entity.type
                                in_entity = True
                            else:
                                tag = "I-" + current_entity.type
                        else:
                            tag = "O"
                            in_entity = False

                        whitespace_after = "+"
                        next_token_offset = offset + len(token)
                        sentence_end_offset = sentence_offset + len(sentence)
                        if (
                            next_token_offset < sentence_end_offset
                            and not document_text[next_token_offset].isspace()
                        ):
                            whitespace_after = "-"

                        if len(token) > 0:
                            f.write(" ".join([token, tag, whitespace_after]) + "\n")
                            sentence_had_tokens = True
                    if sentence_had_tokens:
                        f.write("\n")


def segtok_tokenizer(text: str) -> Tuple[List[str], List[int]]:
    tokens = flair.data.segtok_tokenizer(text)
    tokens_text = [i.text for i in tokens]
    tokens_offset = [i.start_pos for i in tokens]

    return tokens_text, tokens_offset


class SciSpacyTokenizer:
    def __init__(self):
        import spacy
        from spacy.lang import char_classes

        def combined_rule_prefixes() -> List[str]:
            """Helper function that returns the prefix pattern for the tokenizer.
               It is a helper function to accomodate spacy tests that only test
               prefixes.
            """
            prefix_punct = char_classes.PUNCT.replace("|", " ")

            prefixes = (
                ["§", "%", "=", r"\+"]
                + char_classes.split_chars(prefix_punct)
                + char_classes.LIST_ELLIPSES
                + char_classes.LIST_QUOTES
                + char_classes.LIST_CURRENCY
                + char_classes.LIST_ICONS
            )
            return prefixes

        infixes = (
            char_classes.LIST_ELLIPSES
            + char_classes.LIST_ICONS
            + [
                r"×",  # added this special x character to tokenize it separately
                r"[\(\)\[\]\{\}]",  # want to split at every bracket
                r"/",  # want to split at every slash
                r"(?<=[0-9])[+\-\*^](?=[0-9-])",
                r"(?<=[{al}])\.(?=[{au}])".format(
                    al=char_classes.ALPHA_LOWER, au=char_classes.ALPHA_UPPER
                ),
                r"(?<=[{a}]),(?=[{a}])".format(a=char_classes.ALPHA),
                r'(?<=[{a}])[?";:=,.]*(?:{h})(?=[{a}])'.format(
                    a=char_classes.ALPHA, h=char_classes.HYPHENS
                ),
                r"(?<=[{a}0-9])[:<>=/](?=[{a}])".format(a=char_classes.ALPHA),
            ]
        )

        prefix_re = spacy.util.compile_prefix_regex(combined_rule_prefixes())
        infix_re = spacy.util.compile_infix_regex(infixes)

        self.nlp = spacy.load(
            "en_core_sci_sm", disable=["tagger", "ner", "parser", "textcat"]
        )
        self.nlp.tokenizer.prefix_search = prefix_re.search
        self.nlp.tokenizer.infix_finditer = infix_re.finditer

    def __call__(self, sentence: str):
        sentence = self.nlp(sentence)
        tokens = [str(tok) for tok in sentence]
        offsets = [tok.idx for tok in sentence]

        return tokens, offsets


class SciSpacySentenceSplitter:
    def __init__(self):
        import spacy

        self.nlp = spacy.load("en_core_sci_sm", disable=["tagger", "ner", "textcat"])

    def __call__(self, text: str):
        doc = self.nlp(text)
        sentences = [str(sent) for sent in doc.sents]
        offsets = [sent.start_char for sent in doc.sents]

        return sentences, offsets


def build_spacy_tokenizer() -> SciSpacyTokenizer:
    try:
        import spacy

        return SciSpacyTokenizer()
    except ImportError:
        raise ValueError(
            "Default tokenizer is scispacy."
            " Install packages 'scispacy' and"
            " 'https://s3-us-west-2.amazonaws.com/ai2-s2-scispacy"
            "/releases/v0.2.4/en_core_sci_sm-0.2.4.tar.gz' via pip"
            " or choose a different tokenizer"
        )


def build_spacy_sentence_splitter() -> SciSpacySentenceSplitter:
    try:
        import spacy

        return SciSpacySentenceSplitter()
    except ImportError:
        raise ValueError(
            "Default sentence splitter is scispacy."
            " Install packages 'scispacy' and"
            "'https://s3-us-west-2.amazonaws.com/ai2-s2-scispacy"
            "/releases/v0.2.4/en_core_sci_sm-0.2.4.tar.gz' via pip"
            " or choose a different sentence splitter"
        )


class HunerDataset(ColumnCorpus, ABC):
    """
    Base class for HUNER datasets.

    Every subclass has to implement the following methods:
      - `to_internal', which reads the complete data set (incl. train, dev, test) and returns the corpus
        as InternalBioNerDataset
      - `split_url', which returns the base url (i.e. without '.train', '.dev', '.test') to the HUNER split files

    For further information see:
      - Weber et al.: 'HUNER: improving biomedical NER with pretraining'
        https://academic.oup.com/bioinformatics/article-abstract/36/1/295/5523847?redirectedFrom=fulltext
      - HUNER github repository:
        https://github.com/hu-ner/huner
    """

    @abstractmethod
    def to_internal(self, data_folder: Path) -> InternalBioNerDataset:
        raise NotImplementedError()

    @staticmethod
    @abstractmethod
    def split_url() -> str:
        raise NotImplementedError()

    def get_corpus_sentence_splitter(self):
        """
            If the corpus has a pre-defined sentence splitting, then this method returns
            the sentence splitter to be used to reconstruct the original splitting.
            If the corpus has no pre-defined sentence splitting None will be returned.
        """
        return None

    def __init__(
        self,
        base_path: Union[str, Path] = None,
        in_memory: bool = True,
        tokenizer: Callable[[str], Tuple[List[str], List[int]]] = None,
        sentence_splitter: Callable[[str], Tuple[List[str], List[int]]] = None,
    ):
        """
        :param base_path: Path to the corpus on your machine
        :param in_memory: If True, keeps dataset in memory giving speedups in training.
        :param tokenizer: Callable that segments a sentence into words,
                          defaults to scispacy
        :param sentence_splitter: Callable that segments a document into sentences,
                                  defaults to scispacy
        """
        if type(base_path) == str:
            base_path: Path = Path(base_path)

        # column format
        columns = {0: "text", 1: "ner", 2: "space-after"}

        # this dataset name
        dataset_name = self.__class__.__name__.lower()

        # default dataset folder is the cache root
        if not base_path:
            base_path = Path(flair.cache_root) / "datasets"
        data_folder = base_path / dataset_name

        train_file = data_folder / "train.conll"
        dev_file = data_folder / "dev.conll"
        test_file = data_folder / "test.conll"

        if not (train_file.exists() and dev_file.exists() and test_file.exists()):
            splits_dir = data_folder / "splits"
            os.makedirs(splits_dir, exist_ok=True)

            cw_tokenizer = tokenizer if tokenizer else build_spacy_tokenizer()

            cw_sentence_splitter = self.get_corpus_sentence_splitter()
            if not cw_sentence_splitter:
                cw_sentence_splitter = (
                    sentence_splitter
                    if sentence_splitter
                    else build_spacy_sentence_splitter()
                )
            else:
                if sentence_splitter:
                    warn(
                        "Ignoring non-default sentence splitter for corpus with predefined sentences"
                    )

            writer = CoNLLWriter(
                tokenizer=cw_tokenizer, sentence_splitter=cw_sentence_splitter
            )

            internal_dataset = self.to_internal(data_folder)

            train_data = self.get_subset(internal_dataset, "train", splits_dir)
            writer.write_to_conll(train_data, train_file)

            dev_data = self.get_subset(internal_dataset, "dev", splits_dir)
            writer.write_to_conll(dev_data, dev_file)

            test_data = self.get_subset(internal_dataset, "test", splits_dir)
            writer.write_to_conll(test_data, test_file)

        super(HunerDataset, self).__init__(
            data_folder, columns, tag_to_bioes="ner", in_memory=in_memory
        )

    def get_subset(self, dataset: InternalBioNerDataset, split: str, split_dir: Path):
        split_file = cached_path(f"{self.split_url()}.{split}", split_dir)

        with split_file.open() as f:
            ids = [l.strip() for l in f if l.strip()]
            ids = sorted(id_ for id_ in ids if id_ in dataset.documents)

        return InternalBioNerDataset(
            documents={k: dataset.documents[k] for k in ids},
            entities_per_document={k: dataset.entities_per_document[k] for k in ids},
        )


class BIO_INFER(ColumnCorpus):
    """
       Original BioInfer corpus

       For further information see Pyysalo et al.:
          BioInfer: a corpus for information extraction in the biomedical domain
          https://bmcbioinformatics.biomedcentral.com/articles/10.1186/1471-2105-8-50
    """

    def __init__(
        self, base_path: Union[str, Path] = None, in_memory: bool = True,
    ):
        """
           :param base_path: Path to the corpus on your machine
           :param in_memory: If True, keeps dataset in memory giving speedups in training.
           """

        if type(base_path) == str:
            base_path: Path = Path(base_path)

        # column format
        columns = {0: "text", 1: "ner"}

        # this dataset name
        dataset_name = self.__class__.__name__.lower()

        # default dataset folder is the cache root
        if not base_path:
            base_path = Path(flair.cache_root) / "datasets"
        data_folder = base_path / dataset_name

        train_file = data_folder / "train.conll"

        if not (train_file.exists()):
            corpus_folder = self.download_dataset(data_folder)
            corpus_data = self.parse_dataset(corpus_folder)

            tokenizer = whitespace_tokenize

            sentence_splitter = sentence_split_one_sentence_per_doc

            conll_writer = CoNLLWriter(
                tokenizer=tokenizer, sentence_splitter=sentence_splitter
            )
            conll_writer.write_to_conll(corpus_data, train_file)

        super(BIO_INFER, self).__init__(
            data_folder, columns, tag_to_bioes="ner", in_memory=in_memory
        )

    @classmethod
    def download_dataset(cls, data_dir: Path) -> Path:
        data_url = "http://mars.cs.utu.fi/BioInfer/files/BioInfer_corpus_1.1.1.zip"
        data_path = cached_path(data_url, data_dir)
        unpack_file(data_path, data_dir)

        return data_dir / "BioInfer_corpus_1.1.1.xml"

    @classmethod
    def parse_dataset(cls, original_file: Path):
        documents = {}
        entities_per_document = {}

        tree = etree.parse(str(original_file))
        sentence_elems = tree.xpath("//sentence")
        for sentence_id, sentence in enumerate(sentence_elems):
            sentence_id = str(sentence_id)
            token_id_to_span = {}
            sentence_text = ""
            entities_per_document[sentence_id] = []

            for token in sentence.xpath(".//token"):
                token_text = "".join(token.xpath(".//subtoken/@text"))
                token_id = ".".join(token.attrib["id"].split(".")[1:])

                if not sentence_text:
                    token_id_to_span[token_id] = (0, len(token_text))
                    sentence_text = token_text
                else:
                    token_id_to_span[token_id] = (
                        len(sentence_text) + 1,
                        len(token_text) + len(sentence_text) + 1,
                    )
                    sentence_text += " " + token_text
            documents[sentence_id] = sentence_text

            entities = [
                e for e in sentence.xpath(".//entity") if not e.attrib["type"].isupper()
            ]  # all caps entity type apparently marks event trigger

            for entity in entities:
                token_nums = []
                entity_character_starts = []
                entity_character_ends = []

                for subtoken in entity.xpath(".//nestedsubtoken"):
                    token_id_parts = subtoken.attrib["id"].split(".")
                    token_id = ".".join(token_id_parts[1:3])

                    token_nums.append(int(token_id_parts[2]))
                    entity_character_starts.append(token_id_to_span[token_id][0])
                    entity_character_ends.append(token_id_to_span[token_id][1])

                if token_nums and entity_character_starts and entity_character_ends:
                    entity_tokens = list(
                        zip(token_nums, entity_character_starts, entity_character_ends)
                    )

                    start_token = entity_tokens[0]
                    last_entity_token = entity_tokens[0]
                    for entity_token in entity_tokens[1:]:
                        if not (entity_token[0] - 1) == last_entity_token[0]:
                            entities_per_document[sentence_id].append(
                                Entity(
                                    char_span=(start_token[1], last_entity_token[2]),
                                    entity_type=entity.attrib["type"],
                                )
                            )
                            start_token = entity_token

                        last_entity_token = entity_token

                    if start_token:
                        entities_per_document[sentence_id].append(
                            Entity(
                                char_span=(start_token[1], last_entity_token[2]),
                                entity_type=entity.attrib["type"],
                            )
                        )

        return InternalBioNerDataset(
            documents=documents, entities_per_document=entities_per_document
        )


class HUNER_GENE_BIO_INFER(HunerDataset):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    @staticmethod
    def split_url() -> str:
        return "https://raw.githubusercontent.com/hu-ner/huner/master/ner_scripts/splits/bioinfer"

    def to_internal(self, data_dir: Path) -> InternalBioNerDataset:
        original_file = BIO_INFER.download_dataset(data_dir)
        corpus = BIO_INFER.parse_dataset(original_file)

        entity_type_mapping = {
            "Individual_protein": GENE_TAG,
            "Gene/protein/RNA": GENE_TAG,
            "Gene": GENE_TAG,
            "DNA_family_or_group": GENE_TAG,
        }

        return filter_and_map_entities(corpus, entity_type_mapping)


class JNLPBA(ColumnCorpus):
    """
        Original corpus of the JNLPBA shared task.

        For further information see Kim et al.:
          Introduction to the Bio-Entity Recognition Task at JNLPBA
          https://www.aclweb.org/anthology/W04-1213.pdf
    """

    def __init__(self, base_path: Union[str, Path] = None, in_memory: bool = True):
        """
        :param base_path: Path to the corpus on your machine
        :param in_memory: If True, keeps dataset in memory giving speedups in training.
        """

        if type(base_path) == str:
            base_path: Path = Path(base_path)

        # column format
        columns = {0: "text", 1: "ner"}

        # this dataset name
        dataset_name = self.__class__.__name__.lower()

        # default dataset folder is the cache root
        if not base_path:
            base_path = Path(flair.cache_root) / "datasets"
        data_folder = base_path / dataset_name

        train_file = data_folder / "train.conll"
        test_file = data_folder / "test.conll"

        if not (train_file.exists() and test_file.exists()):
            download_dir = data_folder / "original"
            os.makedirs(download_dir, exist_ok=True)

            train_data_url = "http://www.nactem.ac.uk/GENIA/current/Shared-tasks/JNLPBA/Train/Genia4ERtraining.tar.gz"
            train_data_path = cached_path(train_data_url, download_dir)
            unpack_file(train_data_path, download_dir)

            train_data_url = "http://www.nactem.ac.uk/GENIA/current/Shared-tasks/JNLPBA/Evaluation/Genia4ERtest.tar.gz"
            train_data_path = cached_path(train_data_url, download_dir)
            unpack_file(train_data_path, download_dir)

            train_file = download_dir / "Genia4ERtask2.iob2"
            shutil.copy(train_file, data_folder / "train.conll")

            test_file = download_dir / "Genia4EReval2.iob2"
            shutil.copy(test_file, data_folder / "test.conll")

        super(JNLPBA, self).__init__(
            data_folder,
            columns,
            tag_to_bioes="ner",
            in_memory=in_memory,
            comment_symbol="#",
        )


class HunerJNLPBA:
    @classmethod
    def download_and_prepare_train(
        cls, data_folder: Path, sentence_tag: str
    ) -> InternalBioNerDataset:
        train_data_url = "http://www.nactem.ac.uk/GENIA/current/Shared-tasks/JNLPBA/Train/Genia4ERtraining.tar.gz"
        train_data_path = cached_path(train_data_url, data_folder)
        unpack_file(train_data_path, data_folder)

        train_input_file = data_folder / "Genia4ERtask2.iob2"
        return cls.read_file(train_input_file, sentence_tag)

    @classmethod
    def download_and_prepare_test(
        cls, data_folder: Path, sentence_tag: str
    ) -> InternalBioNerDataset:
        test_data_url = "http://www.nactem.ac.uk/GENIA/current/Shared-tasks/JNLPBA/Evaluation/Genia4ERtest.tar.gz"
        test_data_path = cached_path(test_data_url, data_folder)
        unpack_file(test_data_path, data_folder)

        test_input_file = data_folder / "Genia4EReval2.iob2"
        return cls.read_file(test_input_file, sentence_tag)

    @classmethod
    def read_file(
        cls, input_iob_file: Path, sentence_tag: str
    ) -> InternalBioNerDataset:
        documents = {}
        entities_per_document = defaultdict(list)

        with open(str(input_iob_file), "r") as file_reader:
            document_id = None
            document_text = None

            entities = []
            entity_type = None
            entity_start = 0

            for line in file_reader:
                line = line.strip()
                if line[:3] == "###":
                    if not (document_id is None and document_text is None):
                        documents[document_id] = document_text
                        entities_per_document[document_id] = entities

                    document_id = line.split(":")[-1]
                    document_text = None

                    entities = []
                    entity_type = None
                    entity_start = 0

                    file_reader.__next__()
                    continue

                if line:
                    parts = line.split()
                    token = parts[0].strip()
                    tag = parts[1].strip()

                    if tag.startswith("B-"):
                        if entity_type is not None:
                            entities.append(
                                Entity((entity_start, len(document_text)), entity_type)
                            )

                        entity_start = len(document_text) + 1 if document_text else 0
                        entity_type = tag[2:]

                    elif tag == "O" and entity_type is not None:
                        entities.append(
                            Entity((entity_start, len(document_text)), entity_type)
                        )
                        entity_type = None

                    document_text = (
                        document_text + " " + token if document_text else token
                    )

                else:
                    document_text += sentence_tag

                    # Edge case: last token starts a new entity
                    if entity_type is not None:
                        entities.append(
                            Entity((entity_start, len(document_text)), entity_type)
                        )

            # Last document in file
            if not (document_id is None and document_text is None):
                documents[document_id] = document_text
                entities_per_document[document_id] = entities

        return InternalBioNerDataset(
            documents=documents, entities_per_document=entities_per_document
        )


class HUNER_GENE_JNLPBA(HunerDataset):
    """
        HUNER version of the JNLPBA corpus containing gene annotations.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    @staticmethod
    def split_url() -> str:
        return "https://raw.githubusercontent.com/hu-ner/huner/master/ner_scripts/splits/genia"

    def get_corpus_sentence_splitter(self):
        return sentence_split_at_tag

    def to_internal(self, data_dir: Path) -> InternalBioNerDataset:
        orig_folder = data_dir / "original"
        os.makedirs(str(orig_folder), exist_ok=True)

        train_data = HunerJNLPBA.download_and_prepare_train(orig_folder, SENTENCE_TAG)
        train_data = filter_and_map_entities(train_data, {"protein": GENE_TAG})

        test_data = HunerJNLPBA.download_and_prepare_test(orig_folder, SENTENCE_TAG)
        test_data = filter_and_map_entities(test_data, {"protein": GENE_TAG})

        return merge_datasets([train_data, test_data])


class HUNER_CELL_LINE_JNLPBA(HunerDataset):
    """
        HUNER version of the JNLPBA corpus containing cell line annotations.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    @staticmethod
    def split_url() -> str:
        return "https://raw.githubusercontent.com/hu-ner/huner/master/ner_scripts/splits/genia"

    def get_corpus_sentence_splitter(self):
        return sentence_split_at_tag

    def to_internal(self, data_dir: Path) -> InternalBioNerDataset:
        download_folder = data_dir / "original"
        os.makedirs(str(download_folder), exist_ok=True)

        train_data = HunerJNLPBA.download_and_prepare_train(
            download_folder, SENTENCE_TAG
        )
        train_data = filter_and_map_entities(train_data, {"cell_line": CELL_LINE_TAG})

        test_data = HunerJNLPBA.download_and_prepare_test(download_folder, SENTENCE_TAG)
        test_data = filter_and_map_entities(test_data, {"cell_line": CELL_LINE_TAG})

        return merge_datasets([train_data, test_data])


class CELL_FINDER(ColumnCorpus):
    """
        Original CellFinder corpus containing cell line, species and gene annotations.

        For futher information see Neves et al.:
            Annotating and evaluating text for stem cell research
            https://pdfs.semanticscholar.org/38e3/75aeeeb1937d03c3c80128a70d8e7a74441f.pdf
    """

    def __init__(
        self,
        base_path: Union[str, Path] = None,
        in_memory: bool = True,
        tokenizer: Callable[[str], Tuple[List[str], List[int]]] = None,
        sentence_splitter: Callable[[str], Tuple[List[str], List[int]]] = None,
    ):
        """
        :param base_path: Path to the corpus on your machine
        :param in_memory: If True, keeps dataset in memory giving speedups in training.
        :param tokenizer: Callable that segments a sentence into words,
                          defaults to scispacy
        :param sentence_splitter: Callable that segments a document into sentences,
                                  defaults to scispacy
        """
        if type(base_path) == str:
            base_path: Path = Path(base_path)

        # column format
        columns = {0: "text", 1: "ner"}

        # this dataset name
        dataset_name = self.__class__.__name__.lower()

        # default dataset folder is the cache root
        if not base_path:
            base_path = Path(flair.cache_root) / "datasets"
        data_folder = base_path / dataset_name

        train_file = data_folder / "train.conll"
        if not (train_file.exists()):
            train_corpus = self.download_and_prepare(data_folder)

            if tokenizer is None:
                tokenizer = build_spacy_tokenizer()

            if sentence_splitter is None:
                sentence_splitter = build_spacy_sentence_splitter()

            writer = CoNLLWriter(
                tokenizer=tokenizer, sentence_splitter=sentence_splitter,
            )
            writer.write_to_conll(train_corpus, train_file)
        super(CELL_FINDER, self).__init__(
            data_folder, columns, tag_to_bioes="ner", in_memory=in_memory
        )

    @classmethod
    def download_and_prepare(cls, data_folder: Path) -> InternalBioNerDataset:
        data_url = "https://www.informatik.hu-berlin.de/de/forschung/gebiete/wbi/resources/cellfinder/cellfinder1_brat.tar.gz"
        data_path = cached_path(data_url, data_folder)
        unpack_file(data_path, data_folder)

        return cls.read_folder(data_folder)

    @classmethod
    def read_folder(cls, data_folder: Path) -> InternalBioNerDataset:
        ann_files = list(data_folder.glob("*.ann"))
        documents = {}
        entities_per_document = defaultdict(list)
        for ann_file in ann_files:
            with ann_file.open() as f_ann, ann_file.with_suffix(".txt").open() as f_txt:
                document_text = f_txt.read().strip()

                document_id = ann_file.stem
                documents[document_id] = document_text

                for line in f_ann:
                    fields = line.strip().split("\t")
                    if not fields:
                        continue
                    ent_type, char_start, char_end = fields[1].split()
                    entities_per_document[document_id].append(
                        Entity(
                            char_span=(int(char_start), int(char_end)),
                            entity_type=ent_type,
                        )
                    )

                    assert document_text[int(char_start) : int(char_end)] == fields[2]

        return InternalBioNerDataset(
            documents=documents, entities_per_document=dict(entities_per_document)
        )


class HUNER_CELL_LINE_CELL_FINDER(HunerDataset):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    @staticmethod
    def split_url() -> str:
        return "https://raw.githubusercontent.com/hu-ner/huner/master/ner_scripts/splits/cellfinder_cellline"

    def to_internal(self, data_dir: Path) -> InternalBioNerDataset:
        data = CELL_FINDER.download_and_prepare(data_dir)
        data = filter_and_map_entities(data, {"CellLine": CELL_LINE_TAG})

        return data


class HUNER_SPECIES_CELL_FINDER(HunerDataset):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    @staticmethod
    def split_url() -> str:
        return "https://raw.githubusercontent.com/hu-ner/huner/master/ner_scripts/splits/cellfinder_species"

    def to_internal(self, data_dir: Path) -> InternalBioNerDataset:
        data = CELL_FINDER.download_and_prepare(data_dir)
        data = filter_and_map_entities(data, {"Species": SPECIES_TAG})

        return data


class HUNER_GENE_CELL_FINDER(HunerDataset):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    @staticmethod
    def split_url() -> str:
        return "https://raw.githubusercontent.com/hu-ner/huner/master/ner_scripts/splits/cellfinder_protein"

    def to_internal(self, data_dir: Path) -> InternalBioNerDataset:
        data = CELL_FINDER.download_and_prepare(data_dir)
        data = filter_and_map_entities(data, {"GeneProtein": GENE_TAG})

        return data


class MIRNA(ColumnCorpus):
    """
    Original miRNA corpus.

    For further information see Bagewadi et al.:
        Detecting miRNA Mentions and Relations in Biomedical Literature
        https://www.ncbi.nlm.nih.gov/pmc/articles/PMC4602280/
    """

    def __init__(
        self,
        base_path: Union[str, Path] = None,
        in_memory: bool = True,
        tokenizer: Callable[[str], Tuple[List[str], List[int]]] = None,
        sentence_splitter: Callable[[str], Tuple[List[str], List[int]]] = None,
    ):
        """
        :param base_path: Path to the corpus on your machine
        :param in_memory: If True, keeps dataset in memory giving speedups in training.
        :param tokenizer: Callable that segments a sentence into words,
                          defaults to scispacy
        :param sentence_splitter: Callable that segments a document into sentences,
                                  defaults to scispacy
        """
        if type(base_path) == str:
            base_path: Path = Path(base_path)

        # column format
        columns = {0: "text", 1: "ner"}

        # this dataset name
        dataset_name = self.__class__.__name__.lower()

        # default dataset folder is the cache root
        if not base_path:
            base_path = Path(flair.cache_root) / "datasets"
        data_folder = base_path / dataset_name

        train_file = data_folder / "train.conll"
        test_file = data_folder / "test.conll"

        if not (train_file.exists() and test_file.exists()):
            download_folder = data_folder / "original"
            os.makedirs(str(download_folder), exist_ok=True)

            if tokenizer is None:
                tokenizer = build_spacy_tokenizer()

            if sentence_splitter is None:
                sentence_splitter = sentence_split_at_tag

            sentence_separator = (
                SENTENCE_TAG if sentence_splitter == sentence_split_at_tag else " "
            )

            writer = CoNLLWriter(
                tokenizer=tokenizer, sentence_splitter=sentence_splitter,
            )

            train_corpus = self.download_and_prepare_train(
                download_folder, sentence_separator
            )
            writer.write_to_conll(train_corpus, train_file)

            test_corpus = self.download_and_prepare_test(
                download_folder, sentence_separator
            )
            writer.write_to_conll(test_corpus, test_file)

        super(MIRNA, self).__init__(
            data_folder, columns, tag_to_bioes="ner", in_memory=in_memory
        )

    @classmethod
    def download_and_prepare_train(cls, data_folder: Path, sentence_separator: str):
        data_url = "https://www.scai.fraunhofer.de/content/dam/scai/de/downloads/bioinformatik/miRNA/miRNA-Train-Corpus.xml"
        data_path = cached_path(data_url, data_folder)

        return cls.parse_file(data_path, "train", sentence_separator)

    @classmethod
    def download_and_prepare_test(cls, data_folder: Path, sentence_separator):
        data_url = "https://www.scai.fraunhofer.de/content/dam/scai/de/downloads/bioinformatik/miRNA/miRNA-Test-Corpus.xml"
        data_path = cached_path(data_url, data_folder)

        return cls.parse_file(data_path, "test", sentence_separator)

    @classmethod
    def parse_file(
        cls, input_file: Path, split: str, sentence_separator: str
    ) -> InternalBioNerDataset:
        tree = etree.parse(str(input_file))

        documents = {}
        entities_per_document = {}

        for document in tree.xpath(".//document"):
            document_id = document.get("id") + "-" + split
            entities = []

            document_text = ""
            for sentence in document.xpath(".//sentence"):
                if document_text:
                    document_text += sentence_separator

                sentence_offset = len(document_text)
                document_text += (
                    sentence.get("text") if document_text else sentence.get("text")
                )

                for entity in sentence.xpath(".//entity"):
                    start, end = entity.get("charOffset").split("-")
                    entities.append(
                        Entity(
                            (
                                sentence_offset + int(start),
                                sentence_offset + int(end) + 1,
                            ),
                            entity.get("type"),
                        )
                    )

            documents[document_id] = document_text
            entities_per_document[document_id] = entities

        return InternalBioNerDataset(
            documents=documents, entities_per_document=entities_per_document
        )


class HunerMiRNAHelper(object):
    @staticmethod
    def get_mirna_subset(
        dataset: InternalBioNerDataset, split_url: str, split_dir: Path
    ):
        split_file = cached_path(split_url, split_dir)

        with split_file.open() as f:
            ids = [l.strip() for l in f if l.strip()]
            ids = [id + "-train" for id in ids] + [id + "-test" for id in ids]
            ids = sorted(id_ for id_ in ids if id_ in dataset.documents)

        return InternalBioNerDataset(
            documents={k: dataset.documents[k] for k in ids},
            entities_per_document={k: dataset.entities_per_document[k] for k in ids},
        )


class HUNER_GENE_MIRNA(HunerDataset):
    """
        HUNER version of the miRNA corpus containing protein / gene annotations.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    @staticmethod
    def split_url() -> str:
        return "https://raw.githubusercontent.com/hu-ner/huner/master/ner_scripts/splits/miRNA"

    def get_subset(self, dataset: InternalBioNerDataset, split: str, split_dir: Path):
        # In the huner split files there is no information whether a given id originates
        # from the train or test file of the original corpus - so we have to adapt corpus
        # splitting here
        return HunerMiRNAHelper.get_mirna_subset(
            dataset, f"{self.split_url()}.{split}", split_dir
        )

    def get_corpus_sentence_splitter(self):
        return sentence_split_at_tag

    def to_internal(self, data_dir: Path) -> InternalBioNerDataset:
        download_folder = data_dir / "original"
        os.makedirs(str(download_folder), exist_ok=True)

        train_data = MIRNA.download_and_prepare_train(download_folder, SENTENCE_TAG)
        train_data = filter_and_map_entities(train_data, {"Genes/Proteins": GENE_TAG})

        test_data = MIRNA.download_and_prepare_test(download_folder, SENTENCE_TAG)
        test_data = filter_and_map_entities(test_data, {"Genes/Proteins": GENE_TAG})

        return merge_datasets([train_data, test_data])


class HUNER_SPECIES_MIRNA(HunerDataset):
    """
        HUNER version of the miRNA corpus containing species annotations.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    @staticmethod
    def split_url() -> str:
        return "https://raw.githubusercontent.com/hu-ner/huner/master/ner_scripts/splits/miRNA"

    def get_subset(self, dataset: InternalBioNerDataset, split: str, split_dir: Path):
        # In the huner split files there is no information whether a given id originates
        # from the train or test file of the original corpus - so we have to adapt corpus
        # splitting here
        return HunerMiRNAHelper.get_mirna_subset(
            dataset, f"{self.split_url()}.{split}", split_dir
        )

    def get_corpus_sentence_splitter(self):
        return sentence_split_at_tag

    def to_internal(self, data_dir: Path) -> InternalBioNerDataset:
        download_folder = data_dir / "original"
        os.makedirs(str(download_folder), exist_ok=True)

        train_data = MIRNA.download_and_prepare_train(download_folder, SENTENCE_TAG)
        train_data = filter_and_map_entities(train_data, {"Species": SPECIES_TAG})

        test_data = MIRNA.download_and_prepare_test(download_folder, SENTENCE_TAG)
        test_data = filter_and_map_entities(test_data, {"Species": SPECIES_TAG})

        return merge_datasets([train_data, test_data])


class HUNER_DISEASE_MIRNA(HunerDataset):
    """
        HUNER version of the miRNA corpus containing disease annotations.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    @staticmethod
    def split_url() -> str:
        return "https://raw.githubusercontent.com/hu-ner/huner/master/ner_scripts/splits/miRNA"

    def get_subset(self, dataset: InternalBioNerDataset, split: str, split_dir: Path):
        # In the huner split files there is no information whether a given id originates
        # from the train or test file of the original corpus - so we have to adapt corpus
        # splitting here
        return HunerMiRNAHelper.get_mirna_subset(
            dataset, f"{self.split_url()}.{split}", split_dir
        )

    def get_corpus_sentence_splitter(self):
        return sentence_split_at_tag

    def to_internal(self, data_dir: Path) -> InternalBioNerDataset:
        download_folder = data_dir / "original"
        os.makedirs(str(download_folder), exist_ok=True)

        train_data = MIRNA.download_and_prepare_train(download_folder, SENTENCE_TAG)
        train_data = filter_and_map_entities(train_data, {"Diseases": DISEASE_TAG})

        test_data = MIRNA.download_and_prepare_test(download_folder, SENTENCE_TAG)
        test_data = filter_and_map_entities(test_data, {"Diseases": DISEASE_TAG})

        return merge_datasets([train_data, test_data])


class KaewphanCorpusHelper:
    """ Helper class for the corpora from Kaewphan et al., i.e. CLL and Gellus"""

    @staticmethod
    def download_cll_dataset(data_folder: Path):
        data_url = "http://bionlp-www.utu.fi/cell-lines/CLL_corpus.tar.gz"
        data_path = cached_path(data_url, data_folder)
        unpack_file(data_path, data_folder)

    @staticmethod
    def prepare_and_save_dataset(conll_folder: Path, output_file: Path):
        sentences = []
        for file in os.listdir(str(conll_folder)):
            if not file.endswith(".conll"):
                continue

            with open(os.path.join(str(conll_folder), file), "r") as reader:
                sentences.append(reader.read())

        with open(str(output_file), "w", encoding="utf8") as writer:
            writer.writelines(sentences)

    @staticmethod
    def download_gellus_dataset(data_folder: Path):
        data_url = "http://bionlp-www.utu.fi/cell-lines/Gellus_corpus.tar.gz"
        data_path = cached_path(data_url, data_folder)
        unpack_file(data_path, data_folder)

    @staticmethod
    def read_dataset(
        nersuite_folder: Path, sentence_separator: str
    ) -> InternalBioNerDataset:
        documents = {}
        entities_per_document = {}
        for file in os.listdir(str(nersuite_folder)):
            if not file.endswith(".nersuite"):
                continue

            document_id = file.replace(".nersuite", "")

            with open(os.path.join(str(nersuite_folder), file), "r") as reader:
                document_text = ""
                entities = []

                entity_start = None
                entity_type = None

                for line in reader.readlines():
                    line = line.strip()
                    if line:
                        columns = line.split("\t")
                        tag = columns[0]
                        token = columns[3]
                        if tag.startswith("B-"):
                            if entity_type is not None:
                                entities.append(
                                    Entity(
                                        (entity_start, len(document_text)), entity_type
                                    )
                                )

                            entity_start = (
                                len(document_text) + 1 if document_text else 0
                            )
                            entity_type = tag[2:]

                        elif tag == "O" and entity_type is not None:
                            entities.append(
                                Entity((entity_start, len(document_text)), entity_type,)
                            )
                            entity_type = None

                        document_text = (
                            document_text + " " + token if document_text else token
                        )
                    else:
                        # Edge case: last token starts a new entity
                        if entity_type is not None:
                            entities.append(
                                Entity((entity_start, len(document_text)), entity_type)
                            )
                        document_text += sentence_separator

                if document_text.endswith(sentence_separator):
                    document_text = document_text[: -len(sentence_separator)]

                documents[document_id] = document_text
                entities_per_document[document_id] = entities

        return InternalBioNerDataset(
            documents=documents, entities_per_document=entities_per_document
        )


class CLL(ColumnCorpus):
    """
    Original CLL corpus containing cell line annotations.

    For further information, see Kaewphan et al.:
        Cell line name recognition in support of the identification of synthetic lethality in cancer from text
        https://www.ncbi.nlm.nih.gov/pmc/articles/PMC4708107/
    """

    def __init__(self, base_path: Union[str, Path] = None, in_memory: bool = True):
        """
        :param base_path: Path to the corpus on your machine
        :param in_memory: If True, keeps dataset in memory giving speedups in training
        """
        if type(base_path) == str:
            base_path: Path = Path(base_path)

        # column format
        columns = {0: "ner", 1: "text"}

        # this dataset name
        dataset_name = self.__class__.__name__.lower()

        # default dataset folder is the cache root
        if not base_path:
            base_path = Path(flair.cache_root) / "datasets"
        data_folder = base_path / dataset_name

        train_file = data_folder / "train.conll"

        if not (train_file.exists()):
            KaewphanCorpusHelper.download_cll_dataset(data_folder)

            # FIXME: Use nersuite annotations because conll annotation seem to be broken
            conll_folder = data_folder / "CLL-1.0.2" / "conll"
            KaewphanCorpusHelper.prepare_and_save_dataset(conll_folder, train_file)

        super(CLL, self).__init__(
            data_folder, columns, tag_to_bioes="ner", in_memory=in_memory
        )


class HUNER_CELL_LINE_CLL(HunerDataset):
    """
        HUNER version of the CLL corpus containing cell line annotations.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    @staticmethod
    def split_url() -> str:
        return "https://raw.githubusercontent.com/hu-ner/huner/master/ner_scripts/splits/cll"

    def get_corpus_sentence_splitter(self):
        return sentence_split_at_tag

    def to_internal(self, data_dir: Path) -> InternalBioNerDataset:
        KaewphanCorpusHelper.download_cll_dataset(data_dir)

        nersuite_folder = data_dir / "CLL-1.0.2" / "nersuite"
        orig_dataset = KaewphanCorpusHelper.read_dataset(nersuite_folder, SENTENCE_TAG)

        return filter_and_map_entities(orig_dataset, {"CL": CELL_LINE_TAG})


class GELLUS(ColumnCorpus):
    """
    Original Gellus corpus containing cell line annotations.

    For further information, see Kaewphan et al.:
        Cell line name recognition in support of the identification of synthetic lethality in cancer from text
        https://www.ncbi.nlm.nih.gov/pmc/articles/PMC4708107/
    """

    def __init__(self, base_path: Union[str, Path] = None, in_memory: bool = True):
        """
        :param base_path: Path to the corpus on your machine
        :param in_memory: If True, keeps dataset in memory giving speedups in training
        """
        if type(base_path) == str:
            base_path: Path = Path(base_path)

        # column format
        columns = {0: "text", 1: "ner"}

        # this dataset name
        dataset_name = self.__class__.__name__.lower()

        # default dataset folder is the cache root
        if not base_path:
            base_path = Path(flair.cache_root) / "datasets"
        data_folder = base_path / dataset_name

        train_file = data_folder / "train.conll"
        dev_file = data_folder / "dev.conll"
        test_file = data_folder / "test.conll"

        if not (train_file.exists() and dev_file.exists() and test_file.exists()):
            KaewphanCorpusHelper.download_gellus_dataset(data_folder)

            # FIXME: Use nersuite folder instead of conll, since conll annotation seem to be broken
            conll_train = data_folder / "GELLUS-1.0.3" / "conll" / "train"
            KaewphanCorpusHelper.prepare_and_save_dataset(conll_train, train_file)

            conll_dev = data_folder / "GELLUS-1.0.3" / "conll" / "devel"
            KaewphanCorpusHelper.prepare_and_save_dataset(conll_dev, dev_file)

            conll_test = data_folder / "GELLUS-1.0.3" / "conll" / "test"
            KaewphanCorpusHelper.prepare_and_save_dataset(conll_test, test_file)

        super(GELLUS, self).__init__(
            data_folder, columns, tag_to_bioes="ner", in_memory=in_memory
        )


class HUNER_CELL_LINE_GELLUS(HunerDataset):
    """
        HUNER version of the Gellus corpus containing cell line annotations.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    @staticmethod
    def split_url() -> str:
        return "https://raw.githubusercontent.com/hu-ner/huner/master/ner_scripts/splits/gellus"

    def get_corpus_sentence_splitter(self):
        return sentence_split_at_tag

    def to_internal(self, data_dir: Path) -> InternalBioNerDataset:
        KaewphanCorpusHelper.download_gellus_dataset(data_dir)

        splits = []
        for folder in ["train", "devel", "test"]:
            nersuite_folder = data_dir / "GELLUS-1.0.3" / "nersuite" / folder
            splits.append(
                KaewphanCorpusHelper.read_dataset(nersuite_folder, SENTENCE_TAG)
            )

        full_dataset = merge_datasets(splits)
        return filter_and_map_entities(full_dataset, {"Cell-line-name": CELL_LINE_TAG})


class LOCTEXT(ColumnCorpus):
    def __init__(
        self,
        base_path: Union[str, Path] = None,
        in_memory: bool = True,
        tokenizer: Callable[[str], Tuple[List[str], List[int]]] = None,
        sentence_splitter: Callable[[str], Tuple[List[str], List[int]]] = None,
    ):
        """
        :param base_path: Path to the corpus on your machine
        :param in_memory: If True, keeps dataset in memory giving speedups in training.
        :param tokenizer: Callable that segments a sentence into words,
                          defaults to scispacy
        :param sentence_splitter: Callable that segments a document into sentences,
                                  defaults to scispacy
        """
        if type(base_path) == str:
            base_path: Path = Path(base_path)

        # column format
        columns = {0: "text", 1: "ner"}

        # this dataset name
        dataset_name = self.__class__.__name__.lower()

        # default dataset folder is the cache root
        if not base_path:
            base_path = Path(flair.cache_root) / "datasets"
        data_folder = base_path / dataset_name

        train_file = data_folder / "train.conll"

        if not (train_file.exists()):
            self.download_dataset(data_folder)
            full_dataset = self.parse_dataset(data_folder)

            if tokenizer is None:
                tokenizer = build_spacy_tokenizer()

            if sentence_splitter is None:
                sentence_splitter = build_spacy_sentence_splitter()

            conll_writer = CoNLLWriter(
                tokenizer=tokenizer, sentence_splitter=sentence_splitter
            )
            conll_writer.write_to_conll(full_dataset, train_file)

        super(LOCTEXT, self).__init__(
            data_folder, columns, tag_to_bioes="ner", in_memory=in_memory
        )

    @staticmethod
    def download_dataset(data_dir: Path):
        data_url = "http://pubannotation.org/downloads/LocText-annotations.tgz"
        data_path = cached_path(data_url, data_dir)
        unpack_file(data_path, data_dir)

    @staticmethod
    def parse_dataset(data_dir: Path) -> InternalBioNerDataset:
        loctext_json_folder = data_dir / "LocText"

        entity_type_mapping = {
            "go": "protein",
            "uniprot": "protein",
            "taxonomy": "species",
        }

        documents = {}
        entities_per_document = {}

        for file in os.listdir(str(loctext_json_folder)):
            document_id = file.strip(".json")
            entities = []

            with open(os.path.join(str(loctext_json_folder), file), "r") as f_in:
                data = json.load(f_in)
                document_text = data["text"].strip()
                document_text = document_text.replace("\n", " ")

                if "denotations" in data.keys():
                    for ann in data["denotations"]:
                        start = int(ann["span"]["begin"])
                        end = int(ann["span"]["end"])

                        original_entity_type = ann["obj"].split(":")[0]
                        if not original_entity_type in entity_type_mapping:
                            continue

                        entity_type = entity_type_mapping[original_entity_type]
                        entities.append(Entity((start, end), entity_type))

                documents[document_id] = document_text
                entities_per_document[document_id] = entities

        return InternalBioNerDataset(
            documents=documents, entities_per_document=entities_per_document
        )


class HUNER_SPECIES_LOCTEXT(HunerDataset):
    """
        HUNER version of the Loctext corpus containing species annotations.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    @staticmethod
    def split_url() -> str:
        return "https://raw.githubusercontent.com/hu-ner/huner/master/ner_scripts/splits/loctext"

    def to_internal(self, data_dir: Path) -> InternalBioNerDataset:
        LOCTEXT.download_dataset(data_dir)
        dataset = LOCTEXT.parse_dataset(data_dir)

        return filter_and_map_entities(dataset, {"species": SPECIES_TAG})


class HUNER_GENE_LOCTEXT(HunerDataset):
    """
        HUNER version of the Loctext corpus containing protein annotations.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    @staticmethod
    def split_url() -> str:
        return "https://raw.githubusercontent.com/hu-ner/huner/master/ner_scripts/splits/loctext"

    def to_internal(self, data_dir: Path) -> InternalBioNerDataset:
        LOCTEXT.download_dataset(data_dir)
        dataset = LOCTEXT.parse_dataset(data_dir)

        return filter_and_map_entities(dataset, {"protein": GENE_TAG})


class CHEMDNER(ColumnCorpus):
    """
        Original corpus of the CHEMDNER shared task.

        For further information see Krallinger et al.:
          The CHEMDNER corpus of chemicals and drugs and its annotation principles
          https://jcheminf.biomedcentral.com/articles/10.1186/1758-2946-7-S1-S2
    """

    default_dir = Path(flair.cache_root) / "datasets" / "CHEMDNER"

    def __init__(
        self,
        base_path: Union[str, Path] = None,
        in_memory: bool = True,
        tokenizer: Callable[[str], Tuple[List[str], List[int]]] = None,
        sentence_splitter: Callable[[str], Tuple[List[str], List[int]]] = None,
    ):
        """
        :param base_path: Path to the corpus on your machine
        :param in_memory: If True, keeps dataset in memory giving speedups in training.
        :param tokenizer: Callable that segments a sentence into words,
                          defaults to scispacy
        :param sentence_splitter: Callable that segments a document into sentences,
                                  defaults to scispacy
        """

        if type(base_path) == str:
            base_path: Path = Path(base_path)

        # column format
        columns = {0: "text", 1: "ner"}

        # this dataset name
        dataset_name = self.__class__.__name__.lower()

        # default dataset folder is the cache root
        if not base_path:
            # download file is huge => make default_dir visible so that derivative
            # corpora can all use the same download file
            data_folder = self.default_dir
        else:
            data_folder = base_path / dataset_name

        train_file = data_folder / "train.conll"
        dev_file = data_folder / "dev.conll"
        test_file = data_folder / "test.conll"

        if not (train_file.exists() and dev_file.exists() and test_file.exists()):
            download_dir = data_folder / "original"
            os.makedirs(download_dir, exist_ok=True)
            self.download_dataset(download_dir)

            train_data = bioc_to_internal(
                download_dir / "chemdner_corpus" / "training.bioc.xml"
            )
            dev_data = bioc_to_internal(
                download_dir / "chemdner_corpus" / "development.bioc.xml"
            )
            test_data = bioc_to_internal(
                download_dir / "chemdner_corpus" / "evaluation.bioc.xml"
            )

            if tokenizer is None:
                tokenizer = build_spacy_tokenizer()

            if sentence_splitter is None:
                sentence_splitter = build_spacy_sentence_splitter()

            conll_writer = CoNLLWriter(
                tokenizer=tokenizer, sentence_splitter=sentence_splitter
            )
            conll_writer.write_to_conll(train_data, train_file)
            conll_writer.write_to_conll(dev_data, dev_file)
            conll_writer.write_to_conll(test_data, test_file)

        super(CHEMDNER, self).__init__(
            data_folder, columns, tag_to_bioes="ner", in_memory=in_memory
        )

    @staticmethod
    def download_dataset(data_dir: Path):
        data_url = "https://biocreative.bioinformatics.udel.edu/media/store/files/2014/chemdner_corpus.tar.gz"
        data_path = cached_path(data_url, data_dir)
        unpack_file(data_path, data_dir)


class HUNER_CHEMICAL_CHEMDNER(HunerDataset):
    """
        HUNER version of the CHEMDNER corpus containing chemical annotations.
    """

    def __init__(self, *args, download_folder=None, **kwargs):
        self.download_folder = download_folder or CHEMDNER.default_dir / "original"
        super().__init__(*args, **kwargs)

    @staticmethod
    def split_url() -> str:
        return "https://raw.githubusercontent.com/hu-ner/huner/master/ner_scripts/splits/chemdner"

    def to_internal(self, data_dir: Path) -> InternalBioNerDataset:
        os.makedirs(str(self.download_folder), exist_ok=True)
        CHEMDNER.download_dataset(self.download_folder)
        train_data = bioc_to_internal(
            self.download_folder / "chemdner_corpus" / "training.bioc.xml"
        )
        dev_data = bioc_to_internal(
            self.download_folder / "chemdner_corpus" / "development.bioc.xml"
        )
        test_data = bioc_to_internal(
            self.download_folder / "chemdner_corpus" / "evaluation.bioc.xml"
        )
        all_data = merge_datasets([train_data, dev_data, test_data])
        all_data = filter_and_map_entities(
            all_data,
            {
                "ABBREVIATION": CHEMICAL_TAG,
                "FAMILY": CHEMICAL_TAG,
                "FORMULA": CHEMICAL_TAG,
                "IDENTIFIER": CHEMICAL_TAG,
                "MULTIPLE": CHEMICAL_TAG,
                "NO_CLASS": CHEMICAL_TAG,
                "SYSTEMATIC": CHEMICAL_TAG,
                "TRIVIAL": CHEMICAL_TAG,
            },
        )

        return all_data


class IEPA(ColumnCorpus):
    """
        IEPA corpus as provided by http://corpora.informatik.hu-berlin.de/
        (Original corpus is 404)

        For further information see Ding, Berleant, Nettleton, Wurtele:
          Mining MEDLINE: abstracts, sentences, or phrases?
          https://www.ncbi.nlm.nih.gov/pubmed/11928487
    """

    def __init__(
        self,
        base_path: Union[str, Path] = None,
        in_memory: bool = True,
        tokenizer: Callable[[str], Tuple[List[str], List[int]]] = None,
    ):
        """
           :param base_path: Path to the corpus on your machine
           :param in_memory: If True, keeps dataset in memory giving speedups in training.
           :param tokenizer: Callable that segments a sentence into words,
                             defaults to scispacy
           :param sentence_splitter: Callable that segments a document into sentences,
                                     defaults to scispacy
           """

        if type(base_path) == str:
            base_path: Path = Path(base_path)

        # column format
        columns = {0: "text", 1: "ner"}

        # this dataset name
        dataset_name = self.__class__.__name__.lower()

        # default dataset folder is the cache root
        if not base_path:
            base_path = Path(flair.cache_root) / "datasets"
        data_folder = base_path / dataset_name

        train_file = data_folder / "train.conll"

        if not (train_file.exists()):
            download_dir = data_folder / "original"
            os.makedirs(download_dir, exist_ok=True)
            self.download_dataset(download_dir)

            all_data = bioc_to_internal(download_dir / "iepa_bioc.xml")

            if tokenizer is None:
                tokenizer = build_spacy_tokenizer()

            conll_writer = CoNLLWriter(
                tokenizer=tokenizer, sentence_splitter=sentence_split_at_newline
            )
            conll_writer.write_to_conll(all_data, train_file)

        super(IEPA, self).__init__(
            data_folder, columns, tag_to_bioes="ner", in_memory=in_memory
        )

    @staticmethod
    def download_dataset(data_dir: Path):
        data_url = (
            "http://corpora.informatik.hu-berlin.de/corpora/brat2bioc/iepa_bioc.xml.zip"
        )
        data_path = cached_path(data_url, data_dir)
        unpack_file(data_path, data_dir)


class HUNER_GENE_IEPA(HunerDataset):
    """
        HUNER version of the IEPA corpus containing gene annotations.
    """

    def __init__(self, *args, sentence_splitter=None, **kwargs):
        super().__init__(*args, **kwargs)

    @staticmethod
    def split_url() -> str:
        return "https://raw.githubusercontent.com/hu-ner/huner/master/ner_scripts/splits/iepa"

    def get_corpus_sentence_splitter(self):
        return sentence_split_at_newline

    def to_internal(self, data_dir: Path) -> InternalBioNerDataset:
        os.makedirs(str(data_dir), exist_ok=True)
        IEPA.download_dataset(data_dir)

        all_data = bioc_to_internal(data_dir / "iepa_bioc.xml")
        all_data = filter_and_map_entities(all_data, {"Protein": GENE_TAG})

        return all_data


class LINNEAUS(ColumnCorpus):
    """
       Original LINNEAUS corpus containing species annotations.

       For further information see Gerner et al.:
            LINNAEUS: a species name identification system for biomedical literature
            https://www.ncbi.nlm.nih.gov/pubmed/20149233
    """

    def __init__(
        self,
        base_path: Union[str, Path] = None,
        in_memory: bool = True,
        tokenizer: Callable[[str], Tuple[List[str], List[int]]] = None,
    ):
        """
           :param base_path: Path to the corpus on your machine
           :param in_memory: If True, keeps dataset in memory giving speedups in training.
           :param tokenizer: Callable that segments a sentence into words,
                             defaults to scispacy
           """

        if type(base_path) == str:
            base_path: Path = Path(base_path)

        # column format
        columns = {0: "text", 1: "ner"}

        # this dataset name
        dataset_name = self.__class__.__name__.lower()

        # default dataset folder is the cache root
        if not base_path:
            base_path = Path(flair.cache_root) / "datasets"
        data_folder = base_path / dataset_name

        train_file = data_folder / "train.conll"

        if not (train_file.exists()):
            dataset = self.download_and_parse_dataset(data_folder)

            if tokenizer is None:
                tokenizer = build_spacy_tokenizer()

            sentence_splitter = sentence_split_at_tag

            conll_writer = CoNLLWriter(
                tokenizer=tokenizer, sentence_splitter=sentence_splitter
            )
            conll_writer.write_to_conll(dataset, train_file)
        super(LINNEAUS, self).__init__(
            data_folder, columns, tag_to_bioes="ner", in_memory=in_memory
        )

    @staticmethod
    def download_and_parse_dataset(data_dir: Path):
        data_url = "https://iweb.dl.sourceforge.net/project/linnaeus/Corpora/manual-corpus-species-1.0.tar.gz"
        data_path = cached_path(data_url, data_dir)
        unpack_file(data_path, data_dir)

        documents = {}
        entities_per_document = defaultdict(list)

        # Read texts
        texts_directory = data_dir / "manual-corpus-species-1.0" / "txt"
        for filename in os.listdir(str(texts_directory)):
            document_id = filename.strip(".txt")

            with open(os.path.join(str(texts_directory), filename), "r") as file:
                documents[document_id] = file.read().strip()

        # Read annotations
        tag_file = data_dir / "manual-corpus-species-1.0" / "filtered_tags.tsv"
        with open(str(tag_file), "r") as file:
            next(file)  # Ignore header row

            for line in file:
                if not line:
                    continue

                document_id, start, end, text = line.strip().split("\t")[1:5]
                start, end = int(start), int(end)

                entities_per_document[document_id].append(
                    Entity((start, end), SPECIES_TAG)
                )

                document_text = documents[document_id]
                if document_text[start:end] != text:
                    raise AssertionError()

        return InternalBioNerDataset(
            documents=documents, entities_per_document=entities_per_document
        )


class HUNER_SPECIES_LINNEAUS(HunerDataset):
    """
        HUNER version of the LINNEAUS corpus containing species annotations.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    @staticmethod
    def split_url() -> str:
        return "https://raw.githubusercontent.com/hu-ner/huner/master/ner_scripts/splits/linneaus"

    def to_internal(self, data_dir: Path) -> InternalBioNerDataset:
        return LINNEAUS.download_and_parse_dataset(data_dir)


class CDR(ColumnCorpus):
    """
        CDR corpus as provided by https://github.com/JHnlp/BioCreative-V-CDR-Corpus

        For further information see Li et al.:
          BioCreative V CDR task corpus: a resource for chemical disease relation extraction
          https://www.ncbi.nlm.nih.gov/pmc/articles/PMC4860626/
    """

    def __init__(
        self,
        base_path: Union[str, Path] = None,
        in_memory: bool = True,
        tokenizer: Callable[[str], Tuple[List[str], List[int]]] = None,
        sentence_splitter: Callable[[str], Tuple[List[str], List[int]]] = None,
    ):
        """
        :param base_path: Path to the corpus on your machine
        :param in_memory: If True, keeps dataset in memory giving speedups in training.
        :param tokenizer: Callable that segments a sentence into words,
                          defaults to scispacy
        :param sentence_splitter: Callable that segments a document into sentences,
                                  defaults to scispacy
        """

        if type(base_path) == str:
            base_path: Path = Path(base_path)

        # column format
        columns = {0: "text", 1: "ner"}

        # this dataset name
        dataset_name = self.__class__.__name__.lower()

        # default dataset folder is the cache root
        if not base_path:
            base_path = Path(flair.cache_root) / "datasets"
        data_folder = base_path / dataset_name

        train_file = data_folder / "train.conll"
        dev_file = data_folder / "dev.conll"
        test_file = data_folder / "test.conll"

        if not (train_file.exists() and dev_file.exists() and test_file.exists()):
            download_dir = data_folder / "original"
            os.makedirs(download_dir, exist_ok=True)
            self.download_dataset(download_dir)

            train_data = bioc_to_internal(
                download_dir
                / "CDR_Data"
                / "CDR.Corpus.v010516"
                / "CDR_TrainingSet.BioC.xml"
            )
            dev_data = bioc_to_internal(
                download_dir
                / "CDR_Data"
                / "CDR.Corpus.v010516"
                / "CDR_DevelopmentSet.BioC.xml"
            )
            test_data = bioc_to_internal(
                download_dir
                / "CDR_Data"
                / "CDR.Corpus.v010516"
                / "CDR_TestSet.BioC.xml"
            )

            if tokenizer is None:
                tokenizer = build_spacy_tokenizer()

            if sentence_splitter is None:
                sentence_splitter = build_spacy_sentence_splitter()

            conll_writer = CoNLLWriter(
                tokenizer=tokenizer, sentence_splitter=sentence_splitter
            )
            conll_writer.write_to_conll(train_data, train_file)
            conll_writer.write_to_conll(dev_data, dev_file)
            conll_writer.write_to_conll(test_data, test_file)

        super(CDR, self).__init__(
            data_folder, columns, tag_to_bioes="ner", in_memory=in_memory
        )

    @staticmethod
    def download_dataset(data_dir: Path):
        data_url = (
            "https://github.com/JHnlp/BioCreative-V-CDR-Corpus/raw/master/CDR_Data.zip"
        )
        data_path = cached_path(data_url, data_dir)
        unpack_file(data_path, data_dir)


class HUNER_DISEASE_CDR(HunerDataset):
    """
        HUNER version of the IEPA corpus containing disease annotations.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    @staticmethod
    def split_url() -> str:
        return "https://raw.githubusercontent.com/hu-ner/huner/master/ner_scripts/splits/CDRDisease"

    def to_internal(self, data_dir: Path) -> InternalBioNerDataset:
        os.makedirs(str(data_dir), exist_ok=True)
        CDR.download_dataset(data_dir)
        train_data = bioc_to_internal(
            data_dir / "CDR_Data" / "CDR.Corpus.v010516" / "CDR_TrainingSet.BioC.xml"
        )
        dev_data = bioc_to_internal(
            data_dir / "CDR_Data" / "CDR.Corpus.v010516" / "CDR_DevelopmentSet.BioC.xml"
        )
        test_data = bioc_to_internal(
            data_dir / "CDR_Data" / "CDR.Corpus.v010516" / "CDR_TestSet.BioC.xml"
        )
        all_data = merge_datasets([train_data, dev_data, test_data])
        all_data = filter_and_map_entities(all_data, {"Disease": DISEASE_TAG})

        return all_data


class HUNER_CHEMICAL_CDR(HunerDataset):
    """
        HUNER version of the IEPA corpus containing chemical annotations.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    @staticmethod
    def split_url() -> str:
        return "https://raw.githubusercontent.com/hu-ner/huner/master/ner_scripts/splits/CDRChem"

    def to_internal(self, data_dir: Path) -> InternalBioNerDataset:
        os.makedirs(str(data_dir), exist_ok=True)
        CDR.download_dataset(data_dir)
        train_data = bioc_to_internal(
            data_dir / "CDR_Data" / "CDR.Corpus.v010516" / "CDR_TrainingSet.BioC.xml"
        )
        dev_data = bioc_to_internal(
            data_dir / "CDR_Data" / "CDR.Corpus.v010516" / "CDR_DevelopmentSet.BioC.xml"
        )
        test_data = bioc_to_internal(
            data_dir / "CDR_Data" / "CDR.Corpus.v010516" / "CDR_TestSet.BioC.xml"
        )
        all_data = merge_datasets([train_data, dev_data, test_data])
        all_data = filter_and_map_entities(all_data, {"Chemical": CHEMICAL_TAG})

        return all_data


class VARIOME(ColumnCorpus):
    """
        Variome corpus as provided by http://corpora.informatik.hu-berlin.de/corpora/brat2bioc/hvp_bioc.xml.zip
        For further information see Verspoor et al.:
          Annotating the biomedical literature for the human variome
          https://www.ncbi.nlm.nih.gov/pmc/articles/PMC3676157/
    """

    def __init__(
        self,
        base_path: Union[str, Path] = None,
        in_memory: bool = True,
        tokenizer: Callable[[str], Tuple[List[str], List[int]]] = None,
        sentence_splitter: Callable[[str], Tuple[List[str], List[int]]] = None,
    ):
        """
           :param base_path: Path to the corpus on your machine
           :param in_memory: If True, keeps dataset in memory giving speedups in training.
           :param tokenizer: Callable that segments a sentence into words,
                             defaults to scispacy
           :param sentence_splitter: Callable that segments a document into sentences,
                                     defaults to scispacy
           """

        if type(base_path) == str:
            base_path: Path = Path(base_path)

        # column format
        columns = {0: "text", 1: "ner"}

        # this dataset name
        dataset_name = self.__class__.__name__.lower()

        # default dataset folder is the cache root
        if not base_path:
            base_path = Path(flair.cache_root) / "datasets"
        data_folder = base_path / dataset_name

        train_file = data_folder / "train.conll"

        if not (train_file.exists()):
            download_dir = data_folder / "original"
            os.makedirs(download_dir, exist_ok=True)
            self.download_dataset(download_dir)

            all_data = self.parse_corpus(download_dir / "hvp_bioc.xml")

            if tokenizer is None:
                tokenizer = build_spacy_tokenizer()

            if sentence_splitter is None:
                sentence_splitter = build_spacy_sentence_splitter()

            conll_writer = CoNLLWriter(
                tokenizer=tokenizer, sentence_splitter=sentence_splitter
            )
            conll_writer.write_to_conll(all_data, train_file)

        super(VARIOME, self).__init__(
            data_folder, columns, tag_to_bioes="ner", in_memory=in_memory
        )

    @staticmethod
    def download_dataset(data_dir: Path):
        data_url = (
            "http://corpora.informatik.hu-berlin.de/corpora/brat2bioc/hvp_bioc.xml.zip"
        )
        data_path = cached_path(data_url, data_dir)
        unpack_file(data_path, data_dir)

    @staticmethod
    def parse_corpus(corpus_xml: Path) -> InternalBioNerDataset:
        corpus = bioc_to_internal(corpus_xml)

        cleaned_documents = {}
        cleaned_entities_per_document = {}

        for id, document_text in corpus.documents.items():
            entities = corpus.entities_per_document[id]
            original_length = len(document_text)

            text_cleaned = document_text.replace("** IGNORE LINE **\n", "")
            offset = original_length - len(text_cleaned)

            if offset != 0:
                new_entities = []
                for entity in entities:
                    new_start = entity.char_span.start - offset
                    new_end = entity.char_span.stop - offset

                    new_entities.append(Entity((new_start, new_end), entity.type))

                    orig_text = document_text[
                        entity.char_span.start : entity.char_span.stop
                    ]
                    new_text = text_cleaned[new_start:new_end]
                    assert orig_text == new_text

                entities = new_entities
                document_text = text_cleaned

            cleaned_documents[id] = document_text
            cleaned_entities_per_document[id] = entities

        return InternalBioNerDataset(
            documents=cleaned_documents,
            entities_per_document=cleaned_entities_per_document,
        )


class HUNER_GENE_VARIOME(HunerDataset):
    """
        HUNER version of the Variome corpus containing gene annotations.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    @staticmethod
    def split_url() -> str:
        return "https://raw.githubusercontent.com/hu-ner/huner/master/ner_scripts/splits/variome_gene"

    def to_internal(self, data_dir: Path) -> InternalBioNerDataset:
        os.makedirs(str(data_dir), exist_ok=True)
        VARIOME.download_dataset(data_dir)
        all_data = VARIOME.parse_corpus(data_dir / "hvp_bioc.xml")
        all_data = filter_and_map_entities(all_data, {"gene": GENE_TAG})

        return all_data


class HUNER_DISEASE_VARIOME(HunerDataset):
    """
        HUNER version of the Variome corpus containing disease annotations.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    @staticmethod
    def split_url() -> str:
        return "https://raw.githubusercontent.com/hu-ner/huner/master/ner_scripts/splits/variome_disease"

    def to_internal(self, data_dir: Path) -> InternalBioNerDataset:
        os.makedirs(str(data_dir), exist_ok=True)
        VARIOME.download_dataset(data_dir)
        all_data = VARIOME.parse_corpus(data_dir / "hvp_bioc.xml")
        all_data = filter_and_map_entities(
            all_data, {"Disorder": DISEASE_TAG, "disease": DISEASE_TAG}
        )

        return all_data


class HUNER_SPECIES_VARIOME(HunerDataset):
    """
        HUNER version of the Variome corpus containing species annotations.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    @staticmethod
    def split_url() -> str:
        return "https://raw.githubusercontent.com/hu-ner/huner/master/ner_scripts/splits/variome_species"

    def to_internal(self, data_dir: Path) -> InternalBioNerDataset:
        os.makedirs(str(data_dir), exist_ok=True)
        VARIOME.download_dataset(data_dir)
        all_data = VARIOME.parse_corpus(data_dir / "hvp_bioc.xml")
        all_data = filter_and_map_entities(all_data, {"Living_Beings": SPECIES_TAG})

        return all_data


class NCBI_DISEASE(ColumnCorpus):
    """
       Original NCBI disease corpus containing disease annotations.

       For further information see Dogan et al.:
          NCBI disease corpus: a resource for disease name recognition and concept normalization
          https://www.ncbi.nlm.nih.gov/pubmed/24393765
    """

    def __init__(
        self,
        base_path: Union[str, Path] = None,
        in_memory: bool = True,
        tokenizer: Callable[[str], Tuple[List[str], List[int]]] = None,
        sentence_splitter: Callable[[str], Tuple[List[str], List[int]]] = None,
    ):
        """
           :param base_path: Path to the corpus on your machine
           :param in_memory: If True, keeps dataset in memory giving speedups in training.
           :param tokenizer: Callable that segments a sentence into words,
                             defaults to scispacy
           :param sentence_splitter: Callable that segments a document into sentences,
                                     defaults to scispacy
           """

        if type(base_path) == str:
            base_path: Path = Path(base_path)

        # column format
        columns = {0: "text", 1: "ner"}

        # this dataset name
        dataset_name = self.__class__.__name__.lower()

        # default dataset folder is the cache root
        if not base_path:
            base_path = Path(flair.cache_root) / "datasets"
        data_folder = base_path / dataset_name

        train_file = data_folder / "train.conll"
        dev_file = data_folder / "dev.conll"
        test_file = data_folder / "test.conll"

        if not (train_file.exists() and dev_file.exists() and test_file.exists()):
            orig_folder = self.download_corpus(data_folder)

            train_data = self.parse_input_file(orig_folder / "NCBItrainset_patched.txt")
            dev_data = self.parse_input_file(orig_folder / "NCBIdevelopset_corpus.txt")
            test_data = self.parse_input_file(orig_folder / "NCBItestset_corpus.txt")

            if tokenizer is None:
                tokenizer = build_spacy_tokenizer()

            if sentence_splitter is None:
                sentence_splitter = build_spacy_sentence_splitter()

            conll_writer = CoNLLWriter(
                tokenizer=tokenizer, sentence_splitter=sentence_splitter
            )
            conll_writer.write_to_conll(train_data, train_file)
            conll_writer.write_to_conll(dev_data, dev_file)
            conll_writer.write_to_conll(test_data, test_file)

        super(NCBI_DISEASE, self).__init__(
            data_folder, columns, tag_to_bioes="ner", in_memory=in_memory
        )

    @classmethod
    def download_corpus(cls, data_dir: Path) -> Path:
        original_folder = data_dir / "original"
        os.makedirs(str(original_folder), exist_ok=True)

        data_urls = [
            "https://www.ncbi.nlm.nih.gov/CBBresearch/Dogan/DISEASE/NCBItrainset_corpus.zip",
            "https://www.ncbi.nlm.nih.gov/CBBresearch/Dogan/DISEASE/NCBIdevelopset_corpus.zip",
            "https://www.ncbi.nlm.nih.gov/CBBresearch/Dogan/DISEASE/NCBItestset_corpus.zip",
        ]

        for url in data_urls:
            data_path = cached_path(url, original_folder)
            unpack_file(data_path, original_folder)

        # We need to apply a patch to correct the original training file
        orig_train_file = original_folder / "NCBItrainset_corpus.txt"
        patched_train_file = original_folder / "NCBItrainset_patched.txt"
        cls.patch_training_file(orig_train_file, patched_train_file)

        return original_folder

    @staticmethod
    def patch_training_file(orig_train_file: Path, patched_file: Path):
        patch_lines = {
            3249: '10923035\t711\t761\tgeneralized epilepsy and febrile seizures " plus "\tSpecificDisease\tD004829+D003294\n'
        }
        with open(str(orig_train_file), "r") as input:
            with open(str(patched_file), "w") as output:
                line_no = 1

                for line in input:
                    output.write(
                        patch_lines[line_no] if line_no in patch_lines else line
                    )
                    line_no += 1

    @staticmethod
    def parse_input_file(input_file: Path):
        documents = {}
        entities_per_document = {}

        with open(str(input_file), "r") as file:
            document_id = None
            document_text = None
            entities = []

            c = 1
            for line in file:
                line = line.strip()
                if not line:
                    if document_id and document_text:
                        documents[document_id] = document_text
                        entities_per_document[document_id] = entities

                    document_id, document_text, entities = None, None, []
                    c = 1
                    continue
                if c == 1:
                    # Articles title
                    document_text = line.split("|")[2] + " "
                    document_id = line.split("|")[0]
                elif c == 2:
                    # Article abstract
                    document_text += line.split("|")[2]
                else:
                    # Entity annotations
                    columns = line.split("\t")
                    start = int(columns[1])
                    end = int(columns[2])
                    entity_text = columns[3]

                    assert document_text[start:end] == entity_text
                    entities.append(Entity((start, end), DISEASE_TAG))
                c += 1

            if c != 1 and document_id and document_text:
                documents[document_id] = document_text
                entities_per_document[document_id] = entities

        return InternalBioNerDataset(
            documents=documents, entities_per_document=entities_per_document
        )


class HUNER_DISEASE_NCBI(HunerDataset):
    """
        HUNER version of the NCBI corpus containing disease annotations.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    @staticmethod
    def split_url() -> str:
        return "https://raw.githubusercontent.com/hu-ner/huner/master/ner_scripts/splits/ncbi"

    def to_internal(self, data_dir: Path) -> InternalBioNerDataset:
        orig_folder = NCBI_DISEASE.download_corpus(data_dir)

        train_data = NCBI_DISEASE.parse_input_file(
            orig_folder / "NCBItrainset_patched.txt"
        )
        dev_data = NCBI_DISEASE.parse_input_file(
            orig_folder / "NCBIdevelopset_corpus.txt"
        )
        test_data = NCBI_DISEASE.parse_input_file(
            orig_folder / "NCBItestset_corpus.txt"
        )

        return merge_datasets([train_data, dev_data, test_data])


class ScaiCorpus(ColumnCorpus):
    """Base class to support the SCAI chemicals and disease corpora"""

    def __init__(
        self,
        base_path: Union[str, Path] = None,
        in_memory: bool = True,
        sentence_splitter: Callable[[str], Tuple[List[str], List[int]]] = None,
    ):
        """
           :param base_path: Path to the corpus on your machine
           :param in_memory: If True, keeps dataset in memory giving speedups in training.
           :param tokenizer: Callable that segments a sentence into words,
                             defaults to scispacy
           :param sentence_splitter: Callable that segments a document into sentences,
                                     defaults to scispacy
           """

        if type(base_path) == str:
            base_path: Path = Path(base_path)

        # column format
        columns = {0: "text", 1: "ner"}

        # this dataset name
        dataset_name = self.__class__.__name__.lower()

        # default dataset folder is the cache root
        if not base_path:
            base_path = Path(flair.cache_root) / "datasets"
        data_folder = base_path / dataset_name

        train_file = data_folder / "train.conll"

        if not (train_file.exists()):
            dataset_file = self.download_corpus(data_folder)
            train_data = self.parse_input_file(dataset_file)

            if sentence_splitter is None:
                sentence_splitter = build_spacy_sentence_splitter()

            conll_writer = CoNLLWriter(
                tokenizer=whitespace_tokenize, sentence_splitter=sentence_splitter
            )
            conll_writer.write_to_conll(train_data, train_file)

        super(ScaiCorpus, self).__init__(
            data_folder, columns, tag_to_bioes="ner", in_memory=in_memory
        )

    def download_corpus(self, data_folder: Path) -> Path:
        raise NotImplementedError()

    @staticmethod
    def parse_input_file(input_file: Path):
        documents = {}
        entities_per_document = {}

        with open(str(input_file), "r", encoding="iso-8859-1") as file:
            document_id = None
            document_text = None
            entities = []
            entity_type = None

            for line in file:
                line = line.strip()
                if not line:
                    continue

                if line[:3] == "###":
                    # Edge case: last token starts a new entity
                    if entity_type is not None:
                        entities.append(
                            Entity((entity_start, len(document_text)), entity_type)
                        )

                    if not (document_id is None and document_text is None):
                        documents[document_id] = document_text
                        entities_per_document[document_id] = entities

                    document_id = line.strip("#").strip()
                    document_text = None
                    entities = []
                else:
                    columns = line.strip().split("\t")
                    token = columns[0].strip()
                    tag = columns[4].strip().split("|")[1]

                    if tag.startswith("B-"):
                        if entity_type is not None:
                            entities.append(
                                Entity((entity_start, len(document_text)), entity_type)
                            )

                        entity_start = len(document_text) + 1 if document_text else 0
                        entity_type = tag[2:]

                    elif tag == "O" and entity_type is not None:
                        entities.append(
                            Entity((entity_start, len(document_text)), entity_type)
                        )
                        entity_type = None

                    document_text = (
                        document_text + " " + token if document_text else token
                    )

        return InternalBioNerDataset(
            documents=documents, entities_per_document=entities_per_document
        )


class SCAI_CHEMICALS(ScaiCorpus):
    """
       Original SCAI chemicals corpus containing chemical annotations.

       For further information see Kolářik et al.:
            Chemical Names: Terminological Resources and Corpora Annotation
            https://pub.uni-bielefeld.de/record/2603498
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def download_corpus(self, data_dir: Path) -> Path:
        return self.perform_corpus_download(data_dir)

    @staticmethod
    def perform_corpus_download(data_dir: Path) -> Path:
        original_directory = data_dir / "original"
        os.makedirs(str(original_directory), exist_ok=True)

        url = "https://www.scai.fraunhofer.de/content/dam/scai/de/downloads/bioinformatik/Corpora-for-Chemical-Entity-Recognition/chemicals-test-corpus-27-04-2009-v3_iob.gz"
        data_path = cached_path(url, original_directory)
        corpus_file = original_directory / "chemicals-test-corpus-27-04-2009-v3.iob"
        unpack_file(data_path, corpus_file)

        return corpus_file


class SCAI_DISEASE(ScaiCorpus):
    """
       Original SCAI disease corpus containing disease annotations.

       For further information see Gurulingappa et al.:
        An Empirical Evaluation of Resources for the Identification of Diseases and Adverse Effects in Biomedical Literature
        https://pub.uni-bielefeld.de/record/2603398
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def download_corpus(self, data_dir: Path) -> Path:
        return self.perform_corpus_download(data_dir)

    @staticmethod
    def perform_corpus_download(data_dir: Path) -> Path:
        original_directory = data_dir / "original"
        os.makedirs(str(original_directory), exist_ok=True)

        url = "https://www.scai.fraunhofer.de/content/dam/scai/de/downloads/bioinformatik/Disease-ae-corpus.iob"
        data_path = cached_path(url, original_directory)

        return data_path


class HUNER_CHEMICAL_SCAI(HunerDataset):
    """
        HUNER version of the SCAI chemicals corpus containing chemical annotations.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    @staticmethod
    def split_url() -> str:
        return "https://raw.githubusercontent.com/hu-ner/huner/master/ner_scripts/splits/scai_chemicals"

    def to_internal(self, data_dir: Path) -> InternalBioNerDataset:
        original_file = SCAI_CHEMICALS.perform_corpus_download(data_dir)
        corpus = ScaiCorpus.parse_input_file(original_file)

        # Map all entities to chemicals
        entity_mapping = {
            "FAMILY": CHEMICAL_TAG,
            "TRIVIALVAR": CHEMICAL_TAG,
            "PARTIUPAC": CHEMICAL_TAG,
            "TRIVIAL": CHEMICAL_TAG,
            "ABBREVIATION": CHEMICAL_TAG,
            "IUPAC": CHEMICAL_TAG,
            "MODIFIER": CHEMICAL_TAG,
            "SUM": CHEMICAL_TAG,
        }

        return filter_and_map_entities(corpus, entity_mapping)


class HUNER_DISEASE_SCAI(HunerDataset):
    """
        HUNER version of the SCAI chemicals corpus containing chemical annotations.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    @staticmethod
    def split_url() -> str:
        return "https://raw.githubusercontent.com/hu-ner/huner/master/ner_scripts/splits/scai_disease"

    def to_internal(self, data_dir: Path) -> InternalBioNerDataset:
        original_file = SCAI_DISEASE.perform_corpus_download(data_dir)
        corpus = ScaiCorpus.parse_input_file(original_file)

        # Map all entities to disease
        entity_mapping = {"DISEASE": DISEASE_TAG, "ADVERSE": DISEASE_TAG}

        return filter_and_map_entities(corpus, entity_mapping)


class OSIRIS(ColumnCorpus):
    """
       Original OSIRIS corpus containing variation and gene annotations.

       For further information see Furlong et al.:
          Osirisv1.2: a named entity recognition system for sequence variants of genes in biomedical literature
          https://www.ncbi.nlm.nih.gov/pubmed/18251998
    """

    def __init__(
        self,
        base_path: Union[str, Path] = None,
        in_memory: bool = True,
        tokenizer: Callable[[str], Tuple[List[str], List[int]]] = None,
        sentence_splitter: Callable[[str], Tuple[List[str], List[int]]] = None,
        load_original_unfixed_annotation=False,
    ):
        """
           :param base_path: Path to the corpus on your machine
           :param in_memory: If True, keeps dataset in memory giving speedups in training.
           :param tokenizer: Callable that segments a sentence into words,
                             defaults to scispacy
           :param sentence_splitter: Callable that segments a document into sentences,
                                     defaults to scispacy
           :param load_original_unfixed_annotation: The original annotation of Osiris
                erroneously annotates two sentences as a protein. Set to True if you don't
                want the fixed version.
           """

        if type(base_path) == str:
            base_path: Path = Path(base_path)

        # column format
        columns = {0: "text", 1: "ner"}

        # this dataset name
        dataset_name = self.__class__.__name__.lower()

        # default dataset folder is the cache root
        if not base_path:
            base_path = Path(flair.cache_root) / "datasets"
        data_folder = base_path / dataset_name

        train_file = data_folder / "train.conll"

        if not (train_file.exists()):
            corpus_folder = self.download_dataset(data_folder)
            corpus_data = self.parse_dataset(
                corpus_folder, fix_annotation=not load_original_unfixed_annotation
            )

            if tokenizer is None:
                tokenizer = build_spacy_tokenizer()

            if sentence_splitter is None:
                sentence_splitter = build_spacy_sentence_splitter()

            conll_writer = CoNLLWriter(
                tokenizer=tokenizer, sentence_splitter=sentence_splitter
            )
            conll_writer.write_to_conll(corpus_data, train_file)

        super(OSIRIS, self).__init__(
            data_folder, columns, tag_to_bioes="ner", in_memory=in_memory
        )

    @classmethod
    def download_dataset(cls, data_dir: Path) -> Path:
        url = "http://ibi.imim.es/OSIRIScorpusv02.tar"
        data_path = cached_path(url, data_dir)
        unpack_file(data_path, data_dir)

        return data_dir / "OSIRIScorpusv02"

    @classmethod
    def parse_dataset(cls, corpus_folder: Path, fix_annotation=True):
        documents = {}
        entities_per_document = {}

        input_files = [
            file
            for file in os.listdir(str(corpus_folder))
            if file.endswith(".txt") and not file.startswith("README")
        ]
        for text_file in input_files:

            with open(os.path.join(str(corpus_folder), text_file)) as text_reader:
                document_text = text_reader.read()
                if not document_text:
                    continue

                article_parts = document_text.split("\n\n")
                document_id = article_parts[0]
                text_offset = document_text.find(article_parts[1])
                document_text = (article_parts[1] + "  " + article_parts[2]).strip()

            with open(os.path.join(str(corpus_folder), text_file + ".ann")) as ann_file:
                entities = []

                tree = etree.parse(ann_file)
                for annotation in tree.xpath(".//Annotation"):
                    entity_type = annotation.get("type")
                    if entity_type == "file":
                        continue

                    start, end = annotation.get("span").split("..")
                    start, end = int(start), int(end)

                    if (
                        fix_annotation
                        and text_file == "article46.txt"
                        and start == 289
                        and end == 644
                    ):
                        end = 295

                    entities.append(
                        Entity((start - text_offset, end - text_offset), entity_type)
                    )

            documents[document_id] = document_text
            entities_per_document[document_id] = entities

        return InternalBioNerDataset(
            documents=documents, entities_per_document=entities_per_document
        )


class HUNER_GENE_OSIRIS(HunerDataset):
    """
        HUNER version of the OSIRIS corpus containing (only) gene annotations.

    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    @staticmethod
    def split_url() -> str:
        return "https://raw.githubusercontent.com/hu-ner/huner/master/ner_scripts/splits/osiris"

    def to_internal(self, data_dir: Path) -> InternalBioNerDataset:
        original_file = OSIRIS.download_dataset(data_dir)
        corpus = OSIRIS.parse_dataset(original_file)

        entity_type_mapping = {"ge": GENE_TAG}
        return filter_and_map_entities(corpus, entity_type_mapping)


class S800(ColumnCorpus):
    """
        S800 corpus
        For further information see Pafilis et al.:
          The SPECIES and ORGANISMS Resources for Fast and Accurate Identification of Taxonomic Names in Text
          http://www.plosone.org/article/info:doi%2F10.1371%2Fjournal.pone.0065390
    """

    def __init__(
        self,
        base_path: Union[str, Path] = None,
        in_memory: bool = True,
        tokenizer: Callable[[str], Tuple[List[str], List[int]]] = None,
        sentence_splitter: Callable[[str], Tuple[List[str], List[int]]] = None,
    ):
        """
           :param base_path: Path to the corpus on your machine
           :param in_memory: If True, keeps dataset in memory giving speedups in training.
           :param tokenizer: Callable that segments a sentence into words,
                             defaults to scispacy
           :param sentence_splitter: Callable that segments a document into sentences,
                                     defaults to scispacy
           """

        if type(base_path) == str:
            base_path: Path = Path(base_path)

        # column format
        columns = {0: "text", 1: "ner"}

        # this dataset name
        dataset_name = self.__class__.__name__.lower()

        # default dataset folder is the cache root
        if not base_path:
            base_path = Path(flair.cache_root) / "datasets"
        data_folder = base_path / dataset_name

        train_file = data_folder / "train.conll"

        if not (train_file.exists()):
            download_dir = data_folder / "original"
            os.makedirs(download_dir, exist_ok=True)
            self.download_dataset(download_dir)

            all_data = self.parse_dataset(download_dir)

            if tokenizer is None:
                tokenizer = build_spacy_tokenizer()

            if sentence_splitter is None:
                sentence_splitter = build_spacy_sentence_splitter()

            conll_writer = CoNLLWriter(
                tokenizer=tokenizer, sentence_splitter=sentence_splitter
            )
            conll_writer.write_to_conll(all_data, train_file)

        super(S800, self).__init__(
            data_folder, columns, tag_to_bioes="ner", in_memory=in_memory
        )

    @staticmethod
    def download_dataset(data_dir: Path):
        data_url = "https://species.jensenlab.org/files/S800-1.0.tar.gz"
        data_path = cached_path(data_url, data_dir)
        unpack_file(data_path, data_dir)

    @staticmethod
    def parse_dataset(data_dir: Path) -> InternalBioNerDataset:
        entities_per_document = defaultdict(list)
        texts_per_document = {}
        with (data_dir / "S800.tsv").open() as f:
            for line in f:
                fields = line.strip().split("\t")
                if not fields:
                    continue
                fname, pmid = fields[1].split(":")
                start, end = int(fields[2]), int(fields[3])

                if start == end:
                    continue

                entities_per_document[fname].append(Entity((start, end), "Species"))

        for fname in entities_per_document:
            with (data_dir / "abstracts" / fname).with_suffix(".txt").open() as f:
                texts_per_document[fname] = f.read()

        return InternalBioNerDataset(
            documents=texts_per_document, entities_per_document=entities_per_document
        )


class HUNER_SPECIES_S800(HunerDataset):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    @staticmethod
    def split_url() -> str:
        return "https://raw.githubusercontent.com/hu-ner/huner/master/ner_scripts/splits/s800"

    def to_internal(self, data_dir: Path) -> InternalBioNerDataset:
        S800.download_dataset(data_dir)
        data = S800.parse_dataset(data_dir)
        data = filter_and_map_entities(data, {"Species": SPECIES_TAG})

        return data


class GPRO(ColumnCorpus):
    """
       Original GPRO corpus containing gene annotations.

       For further information see:
            https://biocreative.bioinformatics.udel.edu/tasks/biocreative-v/gpro-detailed-task-description/
    """

    def __init__(
        self,
        base_path: Union[str, Path] = None,
        in_memory: bool = True,
        tokenizer: Callable[[str], Tuple[List[str], List[int]]] = None,
        sentence_splitter: Callable[[str], Tuple[List[str], List[int]]] = None,
    ):
        """
           :param base_path: Path to the corpus on your machine
           :param in_memory: If True, keeps dataset in memory giving speedups in training.
           :param tokenizer: Callable that segments a sentence into words,
                             defaults to scispacy
           :param sentence_splitter: Callable that segments a document into sentences,
                                     defaults to scispacy
           """

        if type(base_path) == str:
            base_path: Path = Path(base_path)

        # column format
        columns = {0: "text", 1: "ner"}

        # this dataset name
        dataset_name = self.__class__.__name__.lower()

        # default dataset folder is the cache root
        if not base_path:
            base_path = Path(flair.cache_root) / "datasets"
        data_folder = base_path / dataset_name

        train_file = data_folder / "train.conll"
        dev_file = data_folder / "dev.conll"

        if not (train_file.exists() and dev_file.exists()):
            train_folder = self.download_train_corpus(data_folder)
            train_text_file = train_folder / "chemdner_patents_train_text.txt"
            train_ann_file = train_folder / "chemdner_gpro_gold_standard_train_v02.tsv"
            train_data = self.parse_input_file(train_text_file, train_ann_file)

            dev_folder = self.download_dev_corpus(data_folder)
            dev_text_file = dev_folder / "chemdner_patents_development_text.txt"
            dev_ann_file = dev_folder / "chemdner_gpro_gold_standard_development.tsv"
            dev_data = self.parse_input_file(dev_text_file, dev_ann_file)

            if tokenizer is None:
                tokenizer = build_spacy_tokenizer()

            if sentence_splitter is None:
                sentence_splitter = build_spacy_sentence_splitter()

            conll_writer = CoNLLWriter(
                tokenizer=tokenizer, sentence_splitter=sentence_splitter
            )
            conll_writer.write_to_conll(train_data, train_file)
            conll_writer.write_to_conll(dev_data, dev_file)

        super(GPRO, self).__init__(
            data_folder, columns, tag_to_bioes="ner", in_memory=in_memory
        )

    @classmethod
    def download_train_corpus(cls, data_dir: Path) -> Path:
        corpus_dir = data_dir / "original"
        os.makedirs(str(corpus_dir), exist_ok=True)

        train_url = "https://biocreative.bioinformatics.udel.edu/media/store/files/2015/gpro_training_set_v02.tar.gz"
        data_path = cached_path(train_url, corpus_dir)
        unpack_file(data_path, corpus_dir)

        return corpus_dir / "gpro_training_set_v02"

    @classmethod
    def download_dev_corpus(cls, data_dir) -> Path:
        corpus_dir = data_dir / "original"
        os.makedirs(str(corpus_dir), exist_ok=True)

        dev_url = "https://biocreative.bioinformatics.udel.edu/media/store/files/2015/gpro_development_set.tar.gz"
        data_path = cached_path(dev_url, corpus_dir)
        unpack_file(data_path, corpus_dir)

        return corpus_dir / "gpro_development_set"

    @staticmethod
    def parse_input_file(text_file: Path, ann_file: Path) -> InternalBioNerDataset:
        documents = {}
        entities_per_document = {}

        document_title_length = {}

        with open(str(text_file), "r") as text_reader:
            for line in text_reader:
                if not line:
                    continue

                document_id, title, abstract = line.split("\t")
                documents[document_id] = title + " " + abstract
                document_title_length[document_id] = len(title) + 1

                entities_per_document[document_id] = []

        with open(str(ann_file), "r") as ann_reader:
            for line in ann_reader:
                if not line:
                    continue

                columns = line.split("\t")
                document_id = columns[0]
                start, end = int(columns[2]), int(columns[3])

                if columns[1] == "A":
                    start = start + document_title_length[document_id]
                    end = end + document_title_length[document_id]

                entities_per_document[document_id].append(
                    Entity((start, end), GENE_TAG)
                )

                document_text = documents[document_id]
                assert columns[4] == document_text[start:end]

        return InternalBioNerDataset(
            documents=documents, entities_per_document=entities_per_document
        )


class HUNER_GENE_GPRO(HunerDataset):
    """
        HUNER version of the GPRO corpus containing gene annotations.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    @staticmethod
    def split_url() -> str:
        return "https://raw.githubusercontent.com/hu-ner/huner/master/ner_scripts/splits/gpro"

    def to_internal(self, data_dir: Path) -> InternalBioNerDataset:
        train_folder = GPRO.download_train_corpus(data_dir)
        train_text_file = train_folder / "chemdner_patents_train_text.txt"
        train_ann_file = train_folder / "chemdner_gpro_gold_standard_train_v02.tsv"
        train_data = GPRO.parse_input_file(train_text_file, train_ann_file)

        dev_folder = GPRO.download_dev_corpus(data_dir)
        dev_text_file = dev_folder / "chemdner_patents_development_text.txt"
        dev_ann_file = dev_folder / "chemdner_gpro_gold_standard_development.tsv"
        dev_data = GPRO.parse_input_file(dev_text_file, dev_ann_file)

        return merge_datasets([train_data, dev_data])


class DECA(ColumnCorpus):
    """
          Original DECA corpus containing gene annotations.

          For further information see Wang et al.:
             Disambiguating the species of biomedical named entities using natural language parsers
             https://www.ncbi.nlm.nih.gov/pmc/articles/PMC2828111/
    """

    def __init__(
        self,
        base_path: Union[str, Path] = None,
        in_memory: bool = True,
        tokenizer: Callable[[str], Tuple[List[str], List[int]]] = None,
        sentence_splitter: Callable[[str], Tuple[List[str], List[int]]] = None,
    ):
        """
           :param base_path: Path to the corpus on your machine
           :param in_memory: If True, keeps dataset in memory giving speedups in training.
           :param tokenizer: Callable that segments a sentence into words,
                             defaults to scispacy
           :param sentence_splitter: Callable that segments a document into sentences,
                                     defaults to scispacy
           """

        if type(base_path) == str:
            base_path: Path = Path(base_path)

        # column format
        columns = {0: "text", 1: "ner"}

        # this dataset name
        dataset_name = self.__class__.__name__.lower()

        # default dataset folder is the cache root
        if not base_path:
            base_path = Path(flair.cache_root) / "datasets"
        data_folder = base_path / dataset_name

        train_file = data_folder / "train.conll"

        if not train_file.exists():
            corpus_dir = self.download_corpus(data_folder)
            text_dir = corpus_dir / "text"
            gold_file = corpus_dir / "gold.txt"

            corpus_data = self.parse_corpus(text_dir, gold_file)

            if tokenizer is None:
                tokenizer = build_spacy_tokenizer()

            if sentence_splitter is None:
                sentence_splitter = build_spacy_sentence_splitter()

            conll_writer = CoNLLWriter(
                tokenizer=tokenizer, sentence_splitter=sentence_splitter
            )
            conll_writer.write_to_conll(corpus_data, train_file)

        super(DECA, self).__init__(
            data_folder, columns, tag_to_bioes="ner", in_memory=in_memory
        )

    @classmethod
    def download_corpus(cls, data_dir: Path) -> Path:
        url = "http://www.nactem.ac.uk/deca/species_corpus_0.2.tar.gz"
        data_path = cached_path(url, data_dir)
        unpack_file(data_path, data_dir)

        return data_dir / "species_corpus_0.2"

    @staticmethod
    def parse_corpus(text_dir: Path, gold_file: Path) -> InternalBioNerDataset:
        documents = {}
        entities_per_document = {}

        text_files = [
            file for file in os.listdir(str(text_dir)) if not file.startswith(".")
        ]

        for file in text_files:
            document_id = file.strip(".txt")
            with open(os.path.join(str(text_dir), file), "r") as text_file:
                documents[document_id] = text_file.read().strip()
                entities_per_document[document_id] = []

        with open(str(gold_file), "r") as gold_reader:
            for line in gold_reader:
                if not line:
                    continue
                columns = line.strip().split("\t")

                document_id = columns[0].strip(".txt")
                start, end = int(columns[1]), int(columns[2])

                entities_per_document[document_id].append(
                    Entity((start, end), GENE_TAG)
                )

                document_text = documents[document_id]
                assert document_text[start:end] == columns[3]

        return InternalBioNerDataset(
            documents=documents, entities_per_document=entities_per_document
        )


class HUNER_GENE_DECA(HunerDataset):
    """
        HUNER version of the DECA corpus containing gene annotations.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    @staticmethod
    def split_url() -> str:
        return "https://raw.githubusercontent.com/hu-ner/huner/master/ner_scripts/splits/deca"

    def to_internal(self, data_dir: Path) -> InternalBioNerDataset:
        corpus_dir = DECA.download_corpus(data_dir)
        text_dir = corpus_dir / "text"
        gold_file = corpus_dir / "gold.txt"

        return DECA.parse_corpus(text_dir, gold_file)


class FSU(ColumnCorpus):
    """
          Original FSU corpus containing protein and derived annotations.

          For further information see Hahn et al.:
            A proposal for a configurable silver standard
            https://www.aclweb.org/anthology/W10-1838/
    """

    def __init__(self, base_path: Union[str, Path] = None, in_memory: bool = True):
        """
           :param base_path: Path to the corpus on your machine
           :param in_memory: If True, keeps dataset in memory giving speedups in training.
           :param tokenizer: Callable that segments a sentence into words,
                             defaults to scispacy
           :param sentence_splitter: Callable that segments a document into sentences,
                                     defaults to scispacy
           """

        if type(base_path) == str:
            base_path: Path = Path(base_path)

        # column format
        columns = {0: "text", 1: "ner"}

        # this dataset name
        dataset_name = self.__class__.__name__.lower()

        # default dataset folder is the cache root
        if not base_path:
            base_path = Path(flair.cache_root) / "datasets"
        data_folder = base_path / dataset_name

        train_file = data_folder / "train.conll"

        if not train_file.exists():
            corpus_dir = self.download_corpus(data_folder)
            corpus_data = self.parse_corpus(corpus_dir, SENTENCE_TAG)

            conll_writer = CoNLLWriter(
                tokenizer=whitespace_tokenize, sentence_splitter=sentence_split_at_tag
            )
            conll_writer.write_to_conll(corpus_data, train_file)

        super(FSU, self).__init__(
            data_folder, columns, tag_to_bioes="ner", in_memory=in_memory
        )

    @classmethod
    def download_corpus(cls, data_dir: Path) -> Path:
        url = "https://julielab.de/downloads/resources/fsu_prge_release_v1_0.tgz"
        data_path = cached_path(url, data_dir)
        unpack_file(data_path, data_dir, mode="targz")

        return data_dir / "fsu-prge-release-v1.0"

    @staticmethod
    def parse_corpus(
        corpus_dir: Path, sentence_separator: str
    ) -> InternalBioNerDataset:
        documents = {}
        entities_per_document = {}

        for subcorpus in corpus_dir.iterdir():
            if not subcorpus.is_dir():
                continue
            for doc in (subcorpus / "mmax").iterdir():
                if not doc.is_dir():
                    continue
                try:
                    with open(doc / "Basedata" / "Basedata.xml", "r") as word_f:
                        word_tree = etree.parse(word_f)
                    with open(doc / "Markables" / "sentence.xml", "r") as sentence_f:
                        sentence_tree = etree.parse(sentence_f).getroot()
                    with open(doc / "Markables" / "proteins.xml", "r") as protein_f:
                        protein_tree = etree.parse(protein_f).getroot()
                    with open(doc / "Basedata.uri", "r") as id_f:
                        document_id = id_f.read().strip()
                except FileNotFoundError:
                    # Incomplete article
                    continue
                except XMLSyntaxError:
                    # Invalid XML syntax
                    continue

                word_to_id = {}
                words = []
                for i, token in enumerate(word_tree.xpath(".//word")):
                    words += [token.text]
                    word_to_id[token.get("id")] = i
                word_pos = [(0, 0) for _ in words]

                sentences_id_span = sorted(
                    [
                        (int(sentence.get("id").split("_")[-1]), sentence.get("span"))
                        for sentence in sentence_tree
                    ]
                )

                sentences = []
                for j, sentence in enumerate(sentences_id_span):
                    tmp_sentence = []
                    akt_pos = 0
                    start = word_to_id[sentence[1].split("..")[0]]
                    end = word_to_id[sentence[1].split("..")[1]]
                    for i in range(start, end + 1):
                        tmp_sentence += [words[i]]
                        word_pos[i] = (j, akt_pos)
                        akt_pos += len(words[i]) + 1
                    sentences += [tmp_sentence]

                pre_entities = [[] for _ in sentences]
                for protein in protein_tree:
                    for span in protein.get("span").split(","):
                        start = word_to_id[span.split("..")[0]]
                        end = word_to_id[span.split("..")[-1]]
                        pre_entities[word_pos[start][0]] += [
                            (
                                word_pos[start][1],
                                word_pos[end][1] + len(words[end]),
                                protein.get("proteins"),
                            )
                        ]

                sentences = [" ".join(sentence) for sentence in sentences]
                document = sentence_separator.join(sentences)

                entities = []
                sent_offset = 0
                for sentence, sent_entities in zip(sentences, pre_entities):
                    entities += [
                        Entity(
                            (entity[0] + sent_offset, entity[1] + sent_offset),
                            entity[2],
                        )
                        for entity in sent_entities
                    ]
                    sent_offset += len(sentence) + len(sentence_separator)

                documents[document_id] = document
                entities_per_document[document_id] = entities

        return InternalBioNerDataset(
            documents=documents, entities_per_document=entities_per_document
        )


class HUNER_GENE_FSU(HunerDataset):
    """
        HUNER version of the FSU corpus containing (only) gene annotations.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    @staticmethod
    def split_url() -> str:
        return "https://raw.githubusercontent.com/hu-ner/huner/master/ner_scripts/splits/fsu"

    def get_corpus_sentence_splitter(self):
        return sentence_split_at_tag

    def to_internal(self, data_dir: Path) -> InternalBioNerDataset:
        corpus_dir = FSU.download_corpus(data_dir)
        corpus = FSU.parse_corpus(corpus_dir, SENTENCE_TAG)

        entity_type_mapping = {
            "protein": GENE_TAG,
            "protein_familiy_or_group": GENE_TAG,
            "protein_complex": GENE_TAG,
            "protein_variant": GENE_TAG,
            "protein_enum": GENE_TAG,
        }
        return filter_and_map_entities(corpus, entity_type_mapping)


class CRAFT(ColumnCorpus):
    """
          Original CRAFT corpus containing all but the coreference and sections/typography annotations.

          For further information see Bada et al.: Concept annotation in the craft corpus
    """

    def __init__(
        self,
        base_path: Union[str, Path] = None,
        in_memory: bool = True,
        tokenizer: Callable[[str], Tuple[List[str], List[int]]] = None,
        sentence_splitter: Callable[[str], Tuple[List[str], List[int]]] = None,
    ):
        """
           :param base_path: Path to the corpus on your machine
           :param in_memory: If True, keeps dataset in memory giving speedups in training.
           :param tokenizer: Callable that segments a sentence into words,
                             defaults to scispacy
           :param sentence_splitter: Callable that segments a document into sentences,
                                     defaults to scispacy
           """

        if type(base_path) == str:
            base_path: Path = Path(base_path)

        # column format
        columns = {0: "text", 1: "ner"}

        # this dataset name
        dataset_name = self.__class__.__name__.lower()

        # default dataset folder is the cache root
        if not base_path:
            base_path = Path(flair.cache_root) / "datasets"
        data_folder = base_path / dataset_name

        train_file = data_folder / "train.conll"

        if not train_file.exists():
            corpus_dir = self.download_corpus(data_folder)

            corpus_data = self.parse_corpus(corpus_dir)

            if tokenizer is None:
                tokenizer = build_spacy_tokenizer()

            if sentence_splitter is None:
                sentence_splitter = build_spacy_sentence_splitter()

            conll_writer = CoNLLWriter(
                tokenizer=tokenizer, sentence_splitter=sentence_splitter
            )
            conll_writer.write_to_conll(corpus_data, train_file)

        super(CRAFT, self).__init__(
            data_folder, columns, tag_to_bioes="ner", in_memory=in_memory
        )

    @classmethod
    def download_corpus(cls, data_dir: Path) -> Path:
        url = "http://sourceforge.net/projects/bionlp-corpora/files/CRAFT/v2.0/craft-2.0.tar.gz/download"
        data_path = cached_path(url, data_dir)
        unpack_file(data_path, data_dir, mode="targz")

        return data_dir / "craft-2.0"

    @staticmethod
    def parse_corpus(corpus_dir: Path) -> InternalBioNerDataset:
        documents = {}
        entities_per_document = {}

        text_dir = corpus_dir / "articles" / "txt"
        document_texts = [doc for doc in text_dir.iterdir() if doc.name[-4:] == ".txt"]
        annotation_dirs = [
            path
            for path in (corpus_dir / "xml").iterdir()
            if path.name not in ["sections-and-typography", "coreference"]
        ]

        for doc in Tqdm.tqdm(document_texts, desc="Converting to internal"):
            document_id = doc.name.split(".")[0]

            with open(doc, "r") as f_txt:
                documents[document_id] = f_txt.read()

            entities = []

            for annotation_dir in annotation_dirs:
                with open(
                    annotation_dir / (doc.name + ".annotations.xml"), "r"
                ) as f_ann:
                    ann_tree = etree.parse(f_ann)
                for annotation in ann_tree.xpath("//annotation"):
                    for span in annotation.xpath("span"):
                        start = int(span.get("start"))
                        end = int(span.get("end"))
                        entities += [Entity((start, end), annotation_dir.name)]

            entities_per_document[document_id] = entities

        return InternalBioNerDataset(
            documents=documents, entities_per_document=entities_per_document
        )


class HUNER_GENE_CRAFT(HunerDataset):
    """
        HUNER version of the CRAFT corpus containing (only) gene annotations.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(
            *args, **kwargs,
        )

    @staticmethod
    def split_url() -> str:
        return "https://raw.githubusercontent.com/hu-ner/huner/master/ner_scripts/splits/craft"

    def to_internal(self, data_dir: Path) -> InternalBioNerDataset:
        corpus_dir = CRAFT.download_corpus(data_dir)
        corpus = CRAFT.parse_corpus(corpus_dir)

        entity_type_mapping = {"entrezgene": GENE_TAG, "pr": GENE_TAG}
        return filter_and_map_entities(corpus, entity_type_mapping)


class HUNER_CHEMICAL_CRAFT(HunerDataset):
    """
        HUNER version of the CRAFT corpus containing (only) chemical annotations.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(
            *args, **kwargs,
        )

    @staticmethod
    def split_url() -> str:
        return "https://raw.githubusercontent.com/hu-ner/huner/master/ner_scripts/splits/craft"

    def to_internal(self, data_dir: Path) -> InternalBioNerDataset:
        corpus_dir = CRAFT.download_corpus(data_dir)
        corpus = CRAFT.parse_corpus(corpus_dir)

        entity_type_mapping = {"chebi": CHEMICAL_TAG}
        return filter_and_map_entities(corpus, entity_type_mapping)


class HUNER_SPECIES_CRAFT(HunerDataset):
    """
        HUNER version of the CRAFT corpus containing (only) species annotations.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(
            *args, **kwargs,
        )

    @staticmethod
    def split_url() -> str:
        return "https://raw.githubusercontent.com/hu-ner/huner/master/ner_scripts/splits/craft"

    def to_internal(self, data_dir: Path) -> InternalBioNerDataset:
        corpus_dir = CRAFT.download_corpus(data_dir)
        corpus = CRAFT.parse_corpus(corpus_dir)

        entity_type_mapping = {"ncbitaxon": SPECIES_TAG}
        return filter_and_map_entities(corpus, entity_type_mapping)


class BIOSEMANTICS(ColumnCorpus):
    """
          Original Biosemantics corpus.

          For further information see Akhondi et al.:
            Annotated chemical patent corpus: a gold standard for text mining
            https://www.ncbi.nlm.nih.gov/pmc/articles/PMC4182036/
    """

    def __init__(
        self,
        base_path: Union[str, Path] = None,
        in_memory: bool = True,
        tokenizer: Callable[[str], Tuple[List[str], List[int]]] = None,
        sentence_splitter: Callable[[str], Tuple[List[str], List[int]]] = None,
    ):
        """
        :param base_path: Path to the corpus on your machine
        :param in_memory: If True, keeps dataset in memory giving speedups in training.
        :param tokenizer: Callable that segments a sentence into words,
                          defaults to scispacy
        :param sentence_splitter: Callable that segments a document into sentences,
                                  defaults to scispacy
        """
        if type(base_path) == str:
            base_path: Path = Path(base_path)

        # column format
        columns = {0: "text", 1: "ner"}

        # this dataset name
        dataset_name = self.__class__.__name__.lower()

        # default dataset folder is the cache root
        if not base_path:
            base_path = Path(flair.cache_root) / "datasets"
        data_folder = base_path / dataset_name

        train_file = data_folder / "train.conll"

        if not (train_file.exists()):
            corpus_dir = self.download_dataset(data_folder)
            full_dataset = self.parse_dataset(corpus_dir)

            if tokenizer is None:
                tokenizer = build_spacy_tokenizer()

            if sentence_splitter is None:
                sentence_splitter = build_spacy_sentence_splitter()

            conll_writer = CoNLLWriter(
                tokenizer=tokenizer, sentence_splitter=sentence_splitter
            )
            conll_writer.write_to_conll(full_dataset, train_file)

        super(BIOSEMANTICS, self).__init__(
            data_folder, columns, tag_to_bioes="ner", in_memory=in_memory
        )

    @staticmethod
    def download_dataset(data_dir: Path) -> Path:
        data_url = "http://biosemantics.org/PatentCorpus/Patent_Corpus.rar"
        data_path = cached_path(data_url, data_dir)
        unpack_file(data_path, data_dir)

        return data_dir / "Patent_Corpus"

    @staticmethod
    def parse_dataset(data_dir: Path) -> InternalBioNerDataset:
        base_folder = data_dir / "Full_set"

        dirs = [
            file
            for file in os.listdir(str(base_folder))
            if os.path.isdir(os.path.join(str(base_folder), file))
        ]

        text_files = []
        for directory in dirs:
            text_files += [
                os.path.join(str(base_folder), directory, file)
                for file in os.listdir(os.path.join(str(base_folder), directory))
                if file[-4:] == ".txt"
            ]
        text_files = sorted(text_files)

        documents = {}
        entities_per_document = {}

        for text_file in sorted(text_files):
            document_id = os.path.basename(text_file).split("_")[0]
            with open(text_file, "r") as file_reader:
                file_text = file_reader.read().replace("\n", " ")

            offset = 0
            document_text = ""
            if document_id in documents:
                document_text = documents[document_id] + " "
                offset = len(document_text)

            tmp_document_text = document_text + file_text

            entities = []
            dirty_file = False
            with open(text_file[:-4] + ".ann") as file_reader:
                for line in file_reader:
                    if line[-1] == "\n":
                        line = line[:-1]
                    if not line:
                        continue

                    columns = line.split("\t")
                    mid = columns[1].split()
                    # if len(mid) != 3:
                    #     continue

                    entity_type, start, end = mid[0], mid[1], mid[2]
                    start, end = int(start.split(";")[0]), int(end.split(";")[0])

                    if start == end:
                        continue

                    # Try to fix entity offsets
                    if tmp_document_text[offset + start : offset + end] != columns[2]:
                        alt_text = tmp_document_text[
                            offset + start : offset + start + len(columns[2])
                        ]
                        if alt_text == columns[2]:
                            end = start + len(columns[2])

                    if file_text[start:end] != columns[2]:
                        dirty_file = True
                        continue

                    if tmp_document_text[offset + start : offset + end] != columns[2]:
                        dirty_file = True
                        continue

                    entities.append(Entity((offset + start, offset + end), entity_type))

            if not dirty_file:
                documents[document_id] = tmp_document_text
                if document_id in entities_per_document:
                    entities_per_document[document_id] += entities
                else:
                    entities_per_document[document_id] = entities

        return InternalBioNerDataset(
            documents=documents, entities_per_document=entities_per_document
        )


class HUNER_DISEASE_BIOSEMANTICS(HunerDataset):
    """
        HUNER version of the Biosemantics corpus containing (only) disease annotations.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(
            *args, **kwargs,
        )

    @staticmethod
    def split_url() -> str:
        return "https://raw.githubusercontent.com/hu-ner/huner/master/ner_scripts/splits/bios"

    def to_internal(self, data_dir: Path) -> InternalBioNerDataset:
        corpus_dir = BIOSEMANTICS.download_dataset(data_dir)
        dataset = BIOSEMANTICS.parse_dataset(corpus_dir)

        entity_type_mapping = {"Disease": DISEASE_TAG}
        return filter_and_map_entities(dataset, entity_type_mapping)


class HUNER_CHEMICAL_BIOSEMANTICS(HunerDataset):
    """
        HUNER version of the Biosemantics corpus containing (only) disease annotations.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(
            *args, **kwargs,
        )

    @staticmethod
    def split_url() -> str:
        return "https://raw.githubusercontent.com/hu-ner/huner/master/ner_scripts/splits/bios"

    def to_internal(self, data_dir: Path) -> InternalBioNerDataset:
        corpus_dir = BIOSEMANTICS.download_dataset(data_dir)
        dataset = BIOSEMANTICS.parse_dataset(corpus_dir)

        entity_type_mapping = {
            "M": CHEMICAL_TAG,
            "I": CHEMICAL_TAG,
            "Y": CHEMICAL_TAG,
            "D": CHEMICAL_TAG,
            "B": CHEMICAL_TAG,
            "C": CHEMICAL_TAG,
            "F": CHEMICAL_TAG,
            "R": CHEMICAL_TAG,
            "G": CHEMICAL_TAG,
            "MOA": CHEMICAL_TAG,
        }

        return filter_and_map_entities(dataset, entity_type_mapping)


class BC2GM(ColumnCorpus):
    """
        Original BioCreative-II-GM corpus containing gene annotations.

        For further information see Smith et al.:
            Overview of BioCreative II gene mention recognition
            https://www.ncbi.nlm.nih.gov/pmc/articles/PMC2559986/
    """

    def __init__(
        self,
        base_path: Union[str, Path] = None,
        in_memory: bool = True,
        tokenizer: Callable[[str], Tuple[List[str], List[int]]] = None,
        sentence_splitter: Callable[[str], Tuple[List[str], List[int]]] = None,
    ):
        """
        :param base_path: Path to the corpus on your machine
        :param in_memory: If True, keeps dataset in memory giving speedups in training.
        :param tokenizer: Callable that segments a sentence into words,
                          defaults to scispacy
        :param sentence_splitter: Callable that segments a document into sentences,
                                  defaults to scispacy
        """
        if type(base_path) == str:
            base_path: Path = Path(base_path)

        # column format
        columns = {0: "text", 1: "ner"}

        # this dataset name
        dataset_name = self.__class__.__name__.lower()

        # default dataset folder is the cache root
        if not base_path:
            base_path = Path(flair.cache_root) / "datasets"
        data_folder = base_path / dataset_name

        train_file = data_folder / "train.conll"
        test_file = data_folder / "test.conll"

        if not (train_file.exists() and test_file.exists()):
            data_folder = self.download_dataset(data_folder)
            train_data = self.parse_train_dataset(data_folder)
            test_data = self.parse_test_dataset(data_folder)

            if tokenizer is None:
                tokenizer = build_spacy_tokenizer()

            if sentence_splitter is None:
                sentence_splitter = build_spacy_sentence_splitter()

            conll_writer = CoNLLWriter(
                tokenizer=tokenizer, sentence_splitter=sentence_splitter
            )

            conll_writer.write_to_conll(train_data, train_file)
            conll_writer.write_to_conll(test_data, test_file)

        super(BC2GM, self).__init__(
            data_folder, columns, tag_to_bioes="ner", in_memory=in_memory
        )

    @staticmethod
    def download_dataset(data_dir: Path) -> Path:
        data_url = "https://biocreative.bioinformatics.udel.edu/media/store/files/2011/bc2GMtrain_1.1.tar.gz"
        data_path = cached_path(data_url, data_dir)
        unpack_file(data_path, data_dir)

        data_url = "https://biocreative.bioinformatics.udel.edu/media/store/files/2011/bc2GMtest_1.0.tar.gz"
        data_path = cached_path(data_url, data_dir)
        unpack_file(data_path, data_dir)

        return data_dir

    @classmethod
    def parse_train_dataset(cls, data_folder: Path) -> InternalBioNerDataset:
        train_text_file = data_folder / "bc2geneMention" / "train" / "train.in"
        train_ann_file = data_folder / "bc2geneMention" / "train" / "GENE.eval"

        return cls.parse_dataset(train_text_file, train_ann_file)

    @classmethod
    def parse_test_dataset(cls, data_folder: Path) -> InternalBioNerDataset:
        test_text_file = data_folder / "BC2GM" / "test" / "test.in"
        test_ann_file = data_folder / "BC2GM" / "test" / "GENE.eval"

        return cls.parse_dataset(test_text_file, test_ann_file)

    @staticmethod
    def parse_dataset(text_file: Path, ann_file: Path) -> InternalBioNerDataset:
        documents = {}
        entities_per_document = {}

        with open(str(text_file), "r") as text_file_reader:
            for line in text_file_reader:
                line = line.strip()
                offset = line.find(" ")
                document_id = line[:offset]
                document_text = line[offset + 1 :]
                documents[document_id] = document_text
                entities_per_document[document_id] = []

        with open(str(ann_file), "r") as ann_file_reader:
            for line in ann_file_reader:
                columns = line.strip().split("|")
                document_id = columns[0]
                document_text = documents[document_id]

                start_idx, end_idx = [int(i) for i in columns[1].split()]

                non_whitespaces_chars = 0
                new_start_idx = None
                new_end_idx = None
                for i, char in enumerate(document_text):
                    if char != " ":
                        non_whitespaces_chars += 1
                    if new_start_idx is None and non_whitespaces_chars == start_idx + 1:
                        new_start_idx = i
                    if non_whitespaces_chars == end_idx + 1:
                        new_end_idx = i + 1
                        break

                mention_text = document_text[new_start_idx:new_end_idx]
                if mention_text != columns[2] and mention_text.startswith("/"):
                    # There is still one illegal annotation in the file ..
                    new_start_idx += 1

                entities_per_document[document_id].append(
                    Entity((new_start_idx, new_end_idx), GENE_TAG)
                )

                assert document_text[new_start_idx:new_end_idx] == columns[2]

        return InternalBioNerDataset(
            documents=documents, entities_per_document=entities_per_document
        )


class HUNER_GENE_BC2GM(HunerDataset):
    """
        HUNER version of the BioCreative-II-GM corpus containing gene annotations.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(
            *args, **kwargs,
        )

    @staticmethod
    def split_url() -> str:
        return "https://raw.githubusercontent.com/hu-ner/huner/master/ner_scripts/splits/bc2gm"

    def to_internal(self, data_dir: Path) -> InternalBioNerDataset:
        data_dir = BC2GM.download_dataset(data_dir)
        train_data = BC2GM.parse_train_dataset(data_dir)
        test_data = BC2GM.parse_test_dataset(data_dir)

        return merge_datasets([train_data, test_data])


class CEMP(ColumnCorpus):
    """
       Original CEMP corpus containing chemical annotations.

       For further information see:
            https://biocreative.bioinformatics.udel.edu/tasks/biocreative-v/cemp-detailed-task-description/
    """

    def __init__(
        self,
        base_path: Union[str, Path] = None,
        in_memory: bool = True,
        tokenizer: Callable[[str], Tuple[List[str], List[int]]] = None,
        sentence_splitter: Callable[[str], Tuple[List[str], List[int]]] = None,
    ):
        """
           :param base_path: Path to the corpus on your machine
           :param in_memory: If True, keeps dataset in memory giving speedups in training.
           :param tokenizer: Callable that segments a sentence into words,
                             defaults to scispacy
           :param sentence_splitter: Callable that segments a document into sentences,
                                     defaults to scispacy
           """

        if type(base_path) == str:
            base_path: Path = Path(base_path)

        # column format
        columns = {0: "text", 1: "ner"}

        # this dataset name
        dataset_name = self.__class__.__name__.lower()

        # default dataset folder is the cache root
        if not base_path:
            base_path = Path(flair.cache_root) / "datasets"
        data_folder = base_path / dataset_name

        train_file = data_folder / "train.conll"
        dev_file = data_folder / "dev.conll"

        if not (train_file.exists() and dev_file.exists()):
            train_folder = self.download_train_corpus(data_folder)
            train_text_file = train_folder / "chemdner_patents_train_text.txt"
            train_ann_file = train_folder / "chemdner_cemp_gold_standard_train.tsv"
            train_data = self.parse_input_file(train_text_file, train_ann_file)

            dev_folder = self.download_dev_corpus(data_folder)
            dev_text_file = dev_folder / "chemdner_patents_development_text.txt"
            dev_ann_file = (
                dev_folder / "chemdner_cemp_gold_standard_development_v03.tsv"
            )
            dev_data = self.parse_input_file(dev_text_file, dev_ann_file)

            if tokenizer is None:
                tokenizer = build_spacy_tokenizer()

            if sentence_splitter is None:
                sentence_splitter = build_spacy_sentence_splitter()

            conll_writer = CoNLLWriter(
                tokenizer=tokenizer, sentence_splitter=sentence_splitter
            )
            conll_writer.write_to_conll(train_data, train_file)
            conll_writer.write_to_conll(dev_data, dev_file)

        super(CEMP, self).__init__(
            data_folder, columns, tag_to_bioes="ner", in_memory=in_memory
        )

    @classmethod
    def download_train_corpus(cls, data_dir: Path) -> Path:
        corpus_dir = data_dir / "original"
        os.makedirs(str(corpus_dir), exist_ok=True)

        train_url = "https://biocreative.bioinformatics.udel.edu/media/store/files/2015/cemp_training_set.tar.gz"
        data_path = cached_path(train_url, corpus_dir)
        unpack_file(data_path, corpus_dir)

        return corpus_dir / "cemp_training_set"

    @classmethod
    def download_dev_corpus(cls, data_dir) -> Path:
        corpus_dir = data_dir / "original"
        os.makedirs(str(corpus_dir), exist_ok=True)

        dev_url = "https://biocreative.bioinformatics.udel.edu/media/store/files/2015/cemp_development_set_v03.tar.gz"
        data_path = cached_path(dev_url, corpus_dir)
        unpack_file(data_path, corpus_dir)

        return corpus_dir / "cemp_development_set_v03"

    @staticmethod
    def parse_input_file(text_file: Path, ann_file: Path) -> InternalBioNerDataset:
        documents = {}
        entities_per_document = {}
        document_abstract_length = {}

        with open(str(text_file), "r") as text_reader:
            for line in text_reader:
                if not line:
                    continue

                document_id, title, abstract = line.split("\t")

                # Abstract first, title second to prevent issues with sentence splitting
                documents[document_id] = abstract + " " + title
                document_abstract_length[document_id] = len(abstract) + 1

                entities_per_document[document_id] = []

        with open(str(ann_file), "r") as ann_reader:
            for line in ann_reader:
                if not line:
                    continue

                columns = line.split("\t")
                document_id = columns[0]
                start, end = int(columns[2]), int(columns[3])

                if columns[1] == "T":
                    start = start + document_abstract_length[document_id]
                    end = end + document_abstract_length[document_id]

                entities_per_document[document_id].append(
                    Entity((start, end), columns[5].strip())
                )

                document_text = documents[document_id]
                assert columns[4] == document_text[start:end]

        return InternalBioNerDataset(
            documents=documents, entities_per_document=entities_per_document
        )


class HUNER_CHEMICAL_CEMP(HunerDataset):
    """
        HUNER version of the CEMP corpus containing chemical annotations.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    @staticmethod
    def split_url() -> str:
        return "https://raw.githubusercontent.com/hu-ner/huner/master/ner_scripts/splits/cemp"

    def to_internal(self, data_dir: Path) -> InternalBioNerDataset:
        train_folder = CEMP.download_train_corpus(data_dir)
        train_text_file = train_folder / "chemdner_patents_train_text.txt"
        train_ann_file = train_folder / "chemdner_cemp_gold_standard_train.tsv"
        train_data = CEMP.parse_input_file(train_text_file, train_ann_file)

        dev_folder = CEMP.download_dev_corpus(data_dir)
        dev_text_file = dev_folder / "chemdner_patents_development_text.txt"
        dev_ann_file = dev_folder / "chemdner_cemp_gold_standard_development_v03.tsv"
        dev_data = CEMP.parse_input_file(dev_text_file, dev_ann_file)

        dataset = merge_datasets([train_data, dev_data])
        entity_type_mapping = {
            x: CHEMICAL_TAG
            for x in [
                "ABBREVIATION",
                "FAMILY",
                "FORMULA",
                "IDENTIFIERS",
                "MULTIPLE",
                "SYSTEMATIC",
                "TRIVIAL",
            ]
        }
        return filter_and_map_entities(dataset, entity_type_mapping)


class CHEBI(ColumnCorpus):
    """
       Original CHEBI corpus containing all annotations.

       For further information see Shardlow et al.:
            A New Corpus to Support Text Mining for the Curation of Metabolites in the ChEBI Database
            http://www.lrec-conf.org/proceedings/lrec2018/pdf/229.pdf
    """

    def __init__(
        self,
        base_path: Union[str, Path] = None,
        in_memory: bool = True,
        tokenizer: Callable[[str], Tuple[List[str], List[int]]] = None,
        sentence_splitter: Callable[[str], Tuple[List[str], List[int]]] = None,
        annotator: int = 0,
    ):
        """
        :param base_path: Path to the corpus on your machine
        :param in_memory: If True, keeps dataset in memory giving speedups in training.
        :param tokenizer: Callable that segments a sentence into words,
                          defaults to scispacy
        :param sentence_splitter: Callable that segments a document into sentences,
                                  defaults to scispacy
        :param annotator: The abstracts have been annotated by two annotators, which can be selected by choosing annotator 1 or 2. If annotator is 0, the union of both annotations is used.
        """
        if type(base_path) == str:
            base_path: Path = Path(base_path)

        # column format
        columns = {0: "text", 1: "ner"}

        # this dataset name
        dataset_name = self.__class__.__name__.lower()

        # default dataset folder is the cache root
        if not base_path:
            base_path = Path(flair.cache_root) / "datasets"
        data_folder = base_path / dataset_name

        train_file = data_folder / "train.conll"

        if not (train_file.exists()):
            corpus_dir = self.download_dataset(data_folder)
            full_dataset = self.parse_dataset(corpus_dir, annotator=annotator)

            if tokenizer is None:
                tokenizer = build_spacy_tokenizer()

            if sentence_splitter is None:
                sentence_splitter = build_spacy_sentence_splitter()

            conll_writer = CoNLLWriter(
                tokenizer=tokenizer, sentence_splitter=sentence_splitter
            )
            conll_writer.write_to_conll(full_dataset, train_file)

        super(CHEBI, self).__init__(
            data_folder, columns, tag_to_bioes="ner", in_memory=in_memory
        )

    @staticmethod
    def download_dataset(data_dir: Path) -> Path:
        data_url = "http://www.nactem.ac.uk/chebi/ChEBI.zip"
        data_path = cached_path(data_url, data_dir)
        unpack_file(data_path, data_dir)

        return data_dir / "ChEBI"

    @staticmethod
    def parse_dataset(data_dir: Path, annotator: int) -> InternalBioNerDataset:
        abstract_folder = data_dir / "abstracts"
        fulltext_folder = data_dir / "fullpapers"

        if annotator == 0:
            annotation_dirs = ["Annotator1", "Annotator2"]
        elif annotator <= 2:
            annotation_dirs = [f"Annotator{annotator}"]
        else:
            raise ValueError("Invalid value for annotator")

        documents = {}
        entities_per_document = {}

        abstract_ids = [
            x.name[:-4]
            for x in (abstract_folder / annotation_dirs[0]).iterdir()
            if x.name[-4:] == ".txt"
        ]
        fulltext_ids = [
            x.name[:-4] for x in fulltext_folder.iterdir() if x.name[-4:] == ".txt"
        ]

        for abstract_id in abstract_ids:
            abstract_id_output = abstract_id + "_A"
            with open(
                abstract_folder / annotation_dirs[0] / f"{abstract_id}.txt", "r"
            ) as f:
                documents[abstract_id_output] = f.read()

            for annotation_dir in annotation_dirs:
                with open(
                    abstract_folder / annotation_dir / f"{abstract_id}.ann", "r"
                ) as f:
                    entities = CHEBI.get_entities(f)
            entities_per_document[abstract_id_output] = entities

        for fulltext_id in fulltext_ids:
            fulltext_id_output = fulltext_id + "_F"
            with open(fulltext_folder / f"{fulltext_id}.txt", "r") as f:
                documents[fulltext_id_output] = f.read()

            with open(fulltext_folder / f"{fulltext_id}.ann", "r") as f:
                entities = CHEBI.get_entities(f)
            entities_per_document[fulltext_id_output] = entities

        return InternalBioNerDataset(
            documents=documents, entities_per_document=entities_per_document
        )

    @staticmethod
    def get_entities(f):
        entities = []
        for line in f:
            if not line.strip() or line[0] != "T":
                continue
            parts = line.split("\t")[1].split()
            entity_type = parts[0]
            char_offsets = " ".join(parts[1:])
            for start_end in char_offsets.split(";"):
                start, end = start_end.split(" ")
                entities += [Entity((int(start), int(end)), entity_type)]

        return entities


class HUNER_CHEMICAL_CHEBI(HunerDataset):
    """
        HUNER version of the CHEBI corpus containing chemical annotations.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    @staticmethod
    def split_url() -> str:
        return "https://raw.githubusercontent.com/hu-ner/huner/master/ner_scripts/splits/chebi_new"

    def to_internal(self, data_dir: Path, annotator: int = 0) -> InternalBioNerDataset:
        corpus_dir = CHEBI.download_dataset(data_dir)
        dataset = CHEBI.parse_dataset(corpus_dir, annotator=annotator)
        entity_type_mapping = {"Chemical": CHEMICAL_TAG}
        return filter_and_map_entities(dataset, entity_type_mapping)


class HUNER_GENE_CHEBI(HunerDataset):
    """
        HUNER version of the CHEBI corpus containing gene annotations.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    @staticmethod
    def split_url() -> str:
        return "https://raw.githubusercontent.com/hu-ner/huner/master/ner_scripts/splits/chebi_new"

    def to_internal(self, data_dir: Path, annotator: int = 0) -> InternalBioNerDataset:
        corpus_dir = CHEBI.download_dataset(data_dir)
        dataset = CHEBI.parse_dataset(corpus_dir, annotator=annotator)
        entity_type_mapping = {"Protein": GENE_TAG}
        return filter_and_map_entities(dataset, entity_type_mapping)


class HUNER_SPECIES_CHEBI(HunerDataset):
    """
        HUNER version of the CHEBI corpus containing species annotations.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    @staticmethod
    def split_url() -> str:
        return "https://raw.githubusercontent.com/hu-ner/huner/master/ner_scripts/splits/chebi_new"

    def to_internal(self, data_dir: Path, annotator: int = 0) -> InternalBioNerDataset:
        corpus_dir = CHEBI.download_dataset(data_dir)
        dataset = CHEBI.parse_dataset(corpus_dir, annotator=annotator)
        entity_type_mapping = {"Species": SPECIES_TAG}
        return filter_and_map_entities(dataset, entity_type_mapping)


class BioNLPCorpus(ColumnCorpus):
    """
       Base class for corpora from BioNLP event extraction shared tasks

       For further information see:
            http://2013.bionlp-st.org/Intro
    """

    def __init__(
        self,
        base_path: Union[str, Path] = None,
        in_memory: bool = True,
        tokenizer: Callable[[str], Tuple[List[str], List[int]]] = None,
        sentence_splitter: Callable[[str], Tuple[List[str], List[int]]] = None,
    ):
        """
           :param base_path: Path to the corpus on your machine
           :param in_memory: If True, keeps dataset in memory giving speedups in training.
           :param tokenizer: Callable that segments a sentence into words,
                             defaults to scispacy
           :param sentence_splitter: Callable that segments a document into sentences,
                                     defaults to scispacy
           """

        if type(base_path) == str:
            base_path: Path = Path(base_path)

        # column format
        columns = {0: "text", 1: "ner"}

        # this dataset name
        dataset_name = self.__class__.__name__.lower()

        # default dataset folder is the cache root
        if not base_path:
            base_path = Path(flair.cache_root) / "datasets"
        data_folder = base_path / dataset_name

        train_file = data_folder / "train.conll"
        dev_file = data_folder / "dev.conll"
        test_file = data_folder / "test.conll"

        if not (train_file.exists() and dev_file.exists() and test_file.exists()):
            train_folder, dev_folder, test_folder = self.download_corpus(
                data_folder / "original"
            )
            train_data = self.parse_input_files(train_folder)
            dev_data = self.parse_input_files(dev_folder)
            test_data = self.parse_input_files(test_folder)

            if tokenizer is None:
                tokenizer = build_spacy_tokenizer()

            if sentence_splitter is None:
                sentence_splitter = build_spacy_sentence_splitter()

            conll_writer = CoNLLWriter(
                tokenizer=tokenizer, sentence_splitter=sentence_splitter
            )
            conll_writer.write_to_conll(train_data, train_file)
            conll_writer.write_to_conll(dev_data, dev_file)
            conll_writer.write_to_conll(test_data, test_file)

        super(BioNLPCorpus, self).__init__(
            data_folder, columns, tag_to_bioes="ner", in_memory=in_memory
        )

    @staticmethod
    @abstractmethod
    def download_corpus(data_folder: Path) -> Tuple[Path, Path]:
        pass

    @staticmethod
    def parse_input_files(input_folder: Path) -> InternalBioNerDataset:
        documents = {}
        entities_per_document = {}

        for txt_file in input_folder.glob("*.txt"):
            name = txt_file.with_suffix("").name
            a1_file = txt_file.with_suffix(".a1")
            a2_file = txt_file.with_suffix(".a2")

            with txt_file.open() as f:
                documents[name] = f.read()

            with a1_file.open() as f_a1:
                entities = []

                for line in f_a1:
                    fields = line.strip().split("\t")
                    if fields[0].startswith("T"):
                        ann_type, start, end = fields[1].split()
                        entities.append(
                            Entity(
                                char_span=(int(start), int(end)), entity_type=ann_type
                            )
                        )
                entities_per_document[name] = entities

        return InternalBioNerDataset(
            documents=documents, entities_per_document=entities_per_document
        )


class BIONLP2013_PC(BioNLPCorpus):
    """
    Corpus of the BioNLP'2013 Pathway Curation shared task

    For further information see Ohta et al.
        Overview of the pathway curation (PC) task of bioNLP shared task 2013.
        https://www.aclweb.org/anthology/W13-2009/
    """

    @staticmethod
    def download_corpus(download_folder: Path) -> Tuple[Path, Path, Path]:
        train_url = "http://2013.bionlp-st.org/tasks/BioNLP-ST_2013_PC_training_data.tar.gz?attredirects=0"
        dev_url = "http://2013.bionlp-st.org/tasks/BioNLP-ST_2013_PC_development_data.tar.gz?attredirects=0"
        test_url = "http://2013.bionlp-st.org/tasks/BioNLP-ST_2013_PC_test_data.tar.gz?attredirects=0"

        cached_path(train_url, download_folder)
        cached_path(dev_url, download_folder)
        cached_path(test_url, download_folder)

        unpack_file(
            download_folder / "BioNLP-ST_2013_PC_training_data.tar.gz?attredirects=0",
            download_folder,
            keep=False,
        )
        unpack_file(
            download_folder
            / "BioNLP-ST_2013_PC_development_data.tar.gz?attredirects=0",
            download_folder,
            keep=False,
        )
        unpack_file(
            download_folder / "BioNLP-ST_2013_PC_test_data.tar.gz?attredirects=0",
            download_folder,
            keep=False,
        )

        train_folder = download_folder / "BioNLP-ST_2013_PC_training_data"
        dev_folder = download_folder / "BioNLP-ST_2013_PC_development_data"
        test_folder = download_folder / "BioNLP-ST_2013_PC_test_data"

        return train_folder, dev_folder, test_folder


class BIONLP2013_CG(BioNLPCorpus):
    """
    Corpus of the BioNLP'2013 Cancer Genetics shared task

    For further information see Pyysalo, Ohta & Ananiadou 2013
        Overview of the Cancer Genetics (CG) task of BioNLP Shared Task 2013
        https://www.aclweb.org/anthology/W13-2008/
    """

    @staticmethod
    def download_corpus(download_folder: Path) -> Tuple[Path, Path, Path]:
        train_url = "http://2013.bionlp-st.org/tasks/BioNLP-ST_2013_CG_training_data.tar.gz?attredirects=0"
        dev_url = "http://2013.bionlp-st.org/tasks/BioNLP-ST_2013_CG_development_data.tar.gz?attredirects=0"
        test_url = "http://2013.bionlp-st.org/tasks/BioNLP-ST_2013_CG_test_data.tar.gz?attredirects=0"

        cached_path(train_url, download_folder)
        cached_path(dev_url, download_folder)
        cached_path(test_url, download_folder)

        unpack_file(
            download_folder / "BioNLP-ST_2013_CG_training_data.tar.gz?attredirects=0",
            download_folder,
            keep=False,
            mode="targz",
        )
        unpack_file(
            download_folder
            / "BioNLP-ST_2013_CG_development_data.tar.gz?attredirects=0",
            download_folder,
            keep=False,
            mode="targz",
        )
        unpack_file(
            download_folder / "BioNLP-ST_2013_CG_test_data.tar.gz?attredirects=0",
            download_folder,
            keep=False,
            mode="targz",
        )

        train_folder = download_folder / "BioNLP-ST_2013_CG_training_data"
        dev_folder = download_folder / "BioNLP-ST_2013_CG_development_data"
        test_folder = download_folder / "BioNLP-ST_2013_CG_test_data"

        return train_folder, dev_folder, test_folder


class ANAT_EM(ColumnCorpus):
    """
          Anatomical entity mention recognition

          For further information see Pyysalo and Ananiadou:
            Anatomical entity mention recognition at literature scale
            https://www.ncbi.nlm.nih.gov/pmc/articles/PMC3957068/
            http://nactem.ac.uk/anatomytagger/#AnatEM
       """

    def __init__(
        self,
        base_path: Union[str, Path] = None,
        in_memory: bool = True,
        tokenizer: Callable[[str], Tuple[List[str], List[int]]] = None,
    ):
        """
           :param base_path: Path to the corpus on your machine
           :param in_memory: If True, keeps dataset in memory giving speedups in training.
           :param tokenizer: Callable that segments a sentence into words,
                             defaults to scispacy
           :param sentence_splitter: Callable that segments a document into sentences,
                                     defaults to scispacy
           """
        if type(base_path) == str:
            base_path: Path = Path(base_path)

        # column format
        columns = {0: "text", 1: "ner"}

        # this dataset name
        dataset_name = self.__class__.__name__.lower()

        # default dataset folder is the cache root
        if not base_path:
            base_path = Path(flair.cache_root) / "datasets"
        data_folder = base_path / dataset_name

        train_file = data_folder / "train.conll"
        dev_file = data_folder / "dev.conll"
        test_file = data_folder / "test.conll"

        if not (train_file.exists() and dev_file.exists() and test_file.exists()):
            corpus_folder = self.download_corpus(data_folder)

            train_data = self.parse_input_files(
                corpus_folder / "nersuite" / "train", SENTENCE_TAG
            )
            dev_data = self.parse_input_files(
                corpus_folder / "nersuite" / "devel", SENTENCE_TAG
            )
            test_data = self.parse_input_files(
                corpus_folder / "nersuite" / "test", SENTENCE_TAG
            )

            if tokenizer is None:
                tokenizer = build_spacy_tokenizer()

            conll_writer = CoNLLWriter(
                tokenizer=tokenizer, sentence_splitter=sentence_split_at_tag
            )

            conll_writer.write_to_conll(train_data, train_file)
            conll_writer.write_to_conll(dev_data, dev_file)
            conll_writer.write_to_conll(test_data, test_file)

        super(ANAT_EM, self).__init__(
            data_folder, columns, tag_to_bioes="ner", in_memory=in_memory
        )

    @staticmethod
    @abstractmethod
    def download_corpus(data_folder: Path):
        corpus_url = "http://nactem.ac.uk/anatomytagger/AnatEM-1.0.2.tar.gz"
        corpus_archive = cached_path(corpus_url, data_folder)

        unpack_file(
            corpus_archive, data_folder, keep=True, mode="targz",
        )

        return data_folder / "AnatEM-1.0.2"

    @staticmethod
    def parse_input_files(
        input_dir: Path, sentence_separator: str
    ) -> InternalBioNerDataset:
        documents = {}
        entities_per_document = {}

        input_files = [
            file
            for file in os.listdir(str(input_dir))
            if file.endswith(".nersuite") and not file.startswith("._")
        ]

        for input_file in input_files:
            document_id = input_file.replace(".nersuite", "")
            document_text = ""

            entities = []
            entity_type = None
            entity_start = None

            sent_offset = 0
            last_offset = 0

            input_file = open(str(input_dir / input_file), "r")
            for line in input_file.readlines():
                line = line.strip()
                if line:
                    tag, start, end, word, _, _, _ = line.split("\t")

                    start = int(start) + sent_offset
                    end = int(end) + sent_offset

                    document_text += " " * (start - last_offset)
                    document_text += word

                    if tag.startswith("B-"):
                        if entity_type is not None:
                            entities.append(
                                Entity((entity_start, last_offset), entity_type)
                            )

                        entity_start = start
                        entity_type = tag[2:]

                    elif tag == "O" and entity_type is not None:
                        entities.append(
                            Entity((entity_start, last_offset), entity_type)
                        )
                        entity_type = None

                    last_offset = end

                    assert word == document_text[start:end]

                else:
                    document_text += sentence_separator
                    sent_offset += len(sentence_separator)
                    last_offset += len(sentence_separator)

            documents[document_id] = document_text
            entities_per_document[document_id] = entities

        return InternalBioNerDataset(
            documents=documents, entities_per_document=entities_per_document
        )


class BioBertHelper(ColumnCorpus):
    @staticmethod
    def download_corpora(download_dir: Path):
        from google_drive_downloader import GoogleDriveDownloader as gdd

        gdd.download_file_from_google_drive(
            file_id="1OletxmPYNkz2ltOr9pyT0b0iBtUWxslh",
            dest_path=str(download_dir / "NERdata.zip"),
            unzip=True,
        )

    @staticmethod
    def convert_and_write(download_folder, data_folder, tag_type):
        data_folder.mkdir(parents=True, exist_ok=True)
        with (download_folder / "train.tsv").open() as f_in, (
            data_folder / "train.conll"
        ).open("w") as f_out:
            for line in f_in:
                if not line.strip():
                    f_out.write("\n")
                    continue

                token, tag = line.strip().split("\t")
                if tag != "O":
                    tag = tag + "-" + tag_type
                f_out.write(f"{token} {tag}\n")

        with (download_folder / "devel.tsv").open() as f_in, (
            data_folder / "dev.conll"
        ).open("w") as f_out:
            for line in f_in:
                if not line.strip():
                    f_out.write("\n")
                    continue
                token, tag = line.strip().split("\t")
                if tag != "O":
                    tag = tag + "-" + tag_type
                f_out.write(f"{token} {tag}\n")

        with (download_folder / "test.tsv").open() as f_in, (
            data_folder / "test.conll"
        ).open("w") as f_out:
            for line in f_in:
                if not line.strip():
                    f_out.write("\n")
                    continue
                token, tag = line.strip().split("\t")
                if tag != "O":
                    tag = tag + "-" + tag_type
                f_out.write(f"{token} {tag}\n")


class BIOBERT_CHEMICAL_BC4CHEMD(ColumnCorpus):
    def __init__(self, base_path: Union[str, Path] = None, in_memory: bool = True):
        columns = {0: "text", 1: "ner"}
        # this dataset name
        dataset_name = self.__class__.__name__.lower()

        # default dataset folder is the cache root
        if not base_path:
            base_path = Path(flair.cache_root) / "datasets"

        data_folder = base_path / dataset_name

        train_file = data_folder / "train.conll"
        dev_file = data_folder / "dev.conll"
        test_file = data_folder / "test.conll"

        if not (train_file.exists() and dev_file.exists() and test_file.exists()):
            common_path = base_path / "biobert_common"
            if not (common_path / "BC4CHEMD").exists():
                BioBertHelper.download_corpora(common_path)
            BioBertHelper.convert_and_write(
                common_path / "BC4CHEMD", data_folder, tag_type=CHEMICAL_TAG
            )
        super(BIOBERT_CHEMICAL_BC4CHEMD, self).__init__(
            data_folder, columns, tag_to_bioes="ner", in_memory=in_memory
        )


class BIOBERT_GENE_BC2GM(ColumnCorpus):
    def __init__(self, base_path: Union[str, Path] = None, in_memory: bool = True):
        columns = {0: "text", 1: "ner"}
        # this dataset name
        dataset_name = self.__class__.__name__.lower()

        # default dataset folder is the cache root
        if not base_path:
            base_path = Path(flair.cache_root) / "datasets"

        data_folder = base_path / dataset_name

        train_file = data_folder / "train.conll"
        dev_file = data_folder / "dev.conll"
        test_file = data_folder / "test.conll"

        if not (train_file.exists() and dev_file.exists() and test_file.exists()):
            common_path = base_path / "biobert_common"
            if not (common_path / "BC2GM").exists():
                BioBertHelper.download_corpora(common_path)
            BioBertHelper.convert_and_write(
                common_path / "BC2GM", data_folder, tag_type=GENE_TAG
            )
        super(BIOBERT_GENE_BC2GM, self).__init__(
            data_folder, columns, tag_to_bioes="ner", in_memory=in_memory
        )


class BIOBERT_GENE_JNLPBA(ColumnCorpus):
    def __init__(self, base_path: Union[str, Path] = None, in_memory: bool = True):
        columns = {0: "text", 1: "ner"}
        # this dataset name
        dataset_name = self.__class__.__name__.lower()

        # default dataset folder is the cache root
        if not base_path:
            base_path = Path(flair.cache_root) / "datasets"

        data_folder = base_path / dataset_name

        train_file = data_folder / "train.conll"
        dev_file = data_folder / "dev.conll"
        test_file = data_folder / "test.conll"

        if not (train_file.exists() and dev_file.exists() and test_file.exists()):
            common_path = base_path / "biobert_common"
            if not (common_path / "JNLPBA").exists():
                BioBertHelper.download_corpora(common_path)
            BioBertHelper.convert_and_write(
                common_path / "JNLPBA", data_folder, tag_type=GENE_TAG
            )
        super(BIOBERT_GENE_JNLPBA, self).__init__(
            data_folder, columns, tag_to_bioes="ner", in_memory=in_memory
        )


class BIOBERT_CHEMICAL_BC5CDR(ColumnCorpus):
    def __init__(self, base_path: Union[str, Path] = None, in_memory: bool = True):
        columns = {0: "text", 1: "ner"}
        # this dataset name
        dataset_name = self.__class__.__name__.lower()

        # default dataset folder is the cache root
        if not base_path:
            base_path = Path(flair.cache_root) / "datasets"

        data_folder = base_path / dataset_name

        train_file = data_folder / "train.conll"
        dev_file = data_folder / "dev.conll"
        test_file = data_folder / "test.conll"

        if not (train_file.exists() and dev_file.exists() and test_file.exists()):
            common_path = base_path / "biobert_common"
            if not (common_path / "BC5CDR-chem").exists():
                BioBertHelper.download_corpora(common_path)
            BioBertHelper.convert_and_write(
                common_path / "BC5CDR-chem", data_folder, tag_type=CHEMICAL_TAG
            )
        super(BIOBERT_CHEMICAL_BC5CDR, self).__init__(
            data_folder, columns, tag_to_bioes="ner", in_memory=in_memory
        )


class BIOBERT_DISEASE_BC5CDR(ColumnCorpus):
    def __init__(self, base_path: Union[str, Path] = None, in_memory: bool = True):
        columns = {0: "text", 1: "ner"}
        # this dataset name
        dataset_name = self.__class__.__name__.lower()

        # default dataset folder is the cache root
        if not base_path:
            base_path = Path(flair.cache_root) / "datasets"

        data_folder = base_path / dataset_name

        train_file = data_folder / "train.conll"
        dev_file = data_folder / "dev.conll"
        test_file = data_folder / "test.conll"

        if not (train_file.exists() and dev_file.exists() and test_file.exists()):
            common_path = base_path / "biobert_common"
            if not (common_path / "BC5CDR-disease").exists():
                BioBertHelper.download_corpora(common_path)
            BioBertHelper.convert_and_write(
                common_path / "BC5CDR-disease", data_folder, tag_type=DISEASE_TAG
            )
        super(BIOBERT_DISEASE_BC5CDR, self).__init__(
            data_folder, columns, tag_to_bioes="ner", in_memory=in_memory
        )


class BIOBERT_DISEASE_NCBI(ColumnCorpus):
    def __init__(self, base_path: Union[str, Path] = None, in_memory: bool = True):
        columns = {0: "text", 1: "ner"}
        # this dataset name
        dataset_name = self.__class__.__name__.lower()

        # default dataset folder is the cache root
        if not base_path:
            base_path = Path(flair.cache_root) / "datasets"

        data_folder = base_path / dataset_name

        train_file = data_folder / "train.conll"
        dev_file = data_folder / "dev.conll"
        test_file = data_folder / "test.conll"

        if not (train_file.exists() and dev_file.exists() and test_file.exists()):
            common_path = base_path / "biobert_common"
            if not (common_path / "NCBI-disease").exists():
                BioBertHelper.download_corpora(common_path)
            BioBertHelper.convert_and_write(
                common_path / "NCBI-disease", data_folder, tag_type=DISEASE_TAG
            )
        super(BIOBERT_DISEASE_NCBI, self).__init__(
            data_folder, columns, tag_to_bioes="ner", in_memory=in_memory
        )


class BIOBERT_SPECIES_LINNAEUS(ColumnCorpus):
    def __init__(self, base_path: Union[str, Path] = None, in_memory: bool = True):
        columns = {0: "text", 1: "ner"}
        # this dataset name
        dataset_name = self.__class__.__name__.lower()

        # default dataset folder is the cache root
        if not base_path:
            base_path = Path(flair.cache_root) / "datasets"

        data_folder = base_path / dataset_name

        train_file = data_folder / "train.conll"
        dev_file = data_folder / "dev.conll"
        test_file = data_folder / "test.conll"

        if not (train_file.exists() and dev_file.exists() and test_file.exists()):
            common_path = base_path / "biobert_common"
            if not (common_path / "linnaeus").exists():
                BioBertHelper.download_corpora(common_path)
            BioBertHelper.convert_and_write(
                common_path / "linnaeus", data_folder, tag_type=SPECIES_TAG
            )
        super(BIOBERT_SPECIES_LINNAEUS, self).__init__(
            data_folder, columns, tag_to_bioes="ner", in_memory=in_memory
        )


class BIOBERT_SPECIES_S800(ColumnCorpus):
    def __init__(self, base_path: Union[str, Path] = None, in_memory: bool = True):
        columns = {0: "text", 1: "ner"}
        # this dataset name
        dataset_name = self.__class__.__name__.lower()

        # default dataset folder is the cache root
        if not base_path:
            base_path = Path(flair.cache_root) / "datasets"

        data_folder = base_path / dataset_name

        train_file = data_folder / "train.conll"
        dev_file = data_folder / "dev.conll"
        test_file = data_folder / "test.conll"

        if not (train_file.exists() and dev_file.exists() and test_file.exists()):
            common_path = base_path / "biobert_common"
            if not (common_path / "s800").exists():
                BioBertHelper.download_corpora(common_path)
            BioBertHelper.convert_and_write(
                common_path / "s800", data_folder, tag_type=SPECIES_TAG
            )
        super(BIOBERT_SPECIES_S800, self).__init__(
            data_folder, columns, tag_to_bioes="ner", in_memory=in_memory
        )
