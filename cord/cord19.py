import pickle
import re
import time
from pathlib import Path, PurePath

import ipywidgets as widgets
import numpy as np
import pandas as pd
import requests
from IPython.display import display, clear_output
from rank_bm25 import BM25Okapi
from requests import HTTPError

from cord.core import ifnone, render_html, show_common, describe_dataframe, is_kaggle, CORD_CHALLENGE_PATH, \
    JSON_CATALOGS, find_data_dir, SARS_DATE, SARS_COV_2_DATE
from cord.dates import add_date_diff
from cord.jsonpaper import load_json_paper, load_json_texts
from cord.nlp import get_lda_model, get_topic_vector
from cord.text import preprocess, shorten, summarize


_MINIMUM_SEARCH_SCORE = 2


def get(url, timeout=6):
    try:
        r = requests.get(url, timeout=timeout)
        return r.text
    except ConnectionError:
        print(f'Cannot connect to {url}')
        print(f'Remember to turn Internet ON in the Kaggle notebook settings')
    except HTTPError:
        print('Got http error', r.status, r.text)


_DISPLAY_COLS = ['sha', 'title', 'abstract', 'publish_time', 'authors', 'has_text']
_RESEARCH_PAPERS_SAVE_FILE = 'ResearchPapers.pickle'
_COVID = ['sars-cov-2', '2019-ncov', 'covid-19', 'covid-2019', 'wuhan', 'hubei', 'coronavirus']


# Convert the doi to a url
def doi_url(d):
    if not d:
        return '#'
    return f'http://{d}' if d.startswith('doi.org') else f'http://doi.org/{d}'


_abstract_terms_ = '(Publisher|Abstract|Summary|BACKGROUND|INTRODUCTION)'

# Some titles are is short and unrelated to viruses
# This regex keeps some short titles if they seem relevant
_relevant_re_ = '.*vir.*|.*sars.*|.*mers.*|.*corona.*|.*ncov.*|.*immun.*|.*nosocomial.*'
_relevant_re_ = _relevant_re_ + '.*epidem.*|.*emerg.*|.*vacc.*|.*cytokine.*'


def remove_common_terms(abstract):
    return re.sub(_abstract_terms_, '', abstract)


def start(data):
    return data.copy()


def clean_title(data):
    # Set junk titles to NAN
    title_relevant = data.title.fillna('').str.match(_relevant_re_, case=False)
    title_short = data.title.fillna('').apply(len) < 30
    title_junk = title_short & ~title_relevant
    data.loc[title_junk, 'title'] = ''
    return data


def clean_abstract(data):
    # Set unknowns to NAN
    abstract_unknown = data.abstract == 'Unknown'
    data.loc[abstract_unknown, 'abstract'] = np.nan

    # Fill missing abstract with the title
    data.abstract = data.abstract.fillna(data.title)

    # Remove common terms like publisher
    data.abstract = data.abstract.fillna('').apply(remove_common_terms)

    # Remove the abstract if it is too common
    common_abstracts = show_common(data, 'abstract').query('abstract > 2') \
        .reset_index().query('~(index =="")')['index'].tolist()
    data.loc[data.abstract.isin(common_abstracts), 'abstract'] = ''

    return data


def drop_missing(data):
    missing = (data.published.isnull()) & \
              (data.sha.isnull()) & \
              (data.title == '') & \
              (data.abstract == '')
    return data[~missing].reset_index(drop=True)


def fill_nulls(data):
    data.authors = data.authors.fillna('')
    data.doi = data.doi.fillna('')
    data.journal = data.journal.fillna('')
    data.abstract = data.abstract.fillna('')
    return data


def rename_publish_time(data):
    return data.rename(columns={'publish_time': 'published'})

COVID_TERMS = ['covid', 'sars-?n?cov-?2','2019-ncov', 'novel coronavirus', 'sars coronavirus 2']
COVID_SEARCH = f".*({'|'.join(COVID_TERMS)})"
NOVEL_CORONAVIRUS = '.*novel coronavirus'
WUHAN_OUTBREAK = 'wuhan'


def tag_covid(data):
    """
    Tag all the records that match covid
    :param data:
    :return: data
    """
    abstract = data.abstract.fillna('')
    since_covid = (data.published > SARS_COV_2_DATE) | (data.published.isnull())
    covid_term_match = since_covid & abstract.str.match(COVID_SEARCH, case=False)
    wuhan_outbreak = since_covid & abstract.str.match('.*(wuhan|hubei)', case=False)
    covid_match = covid_term_match | wuhan_outbreak
    data['covid_related'] = False
    data.loc[covid_match, 'covid_related'] = True
    return data


def tag_virus(data):
    VIRUS_SEARCH = f".*(virus|viruses|viral)"
    viral_cond = data.abstract.str.match(VIRUS_SEARCH, case=False)
    data['virus'] = False
    data.loc[viral_cond, 'virus'] = True
    return data


def tag_coronavirus(data):
    corona_cond = data.abstract.str.match(".*corona", case=False)
    data['coronavirus'] = False
    data.loc[corona_cond, 'coronavirus'] = True
    return data


def tag_sars(data):
    sars_cond = data.abstract.str.match(".*sars", case=False)
    sars_not_covid = ~(data.covid_related) & (sars_cond)
    data['sars'] = False
    data.loc[sars_not_covid, 'sars'] = True
    return data


def apply_tags(data):
    print('Applying tags to metadata')
    data = data.pipe(tag_covid)\
        .pipe(tag_virus)\
        .pipe(tag_coronavirus) \
        .pipe(tag_sars)
    return data


def clean_metadata(metadata):
    print('Cleaning metadata')
    return metadata.pipe(start) \
        .pipe(clean_title) \
        .pipe(clean_abstract) \
        .pipe(rename_publish_time) \
        .pipe(add_date_diff) \
        .pipe(drop_missing) \
        .pipe(fill_nulls) \
        .pipe(apply_tags)


def get_json_path(data_path, text_path, sha):
    return Path(data_path) / text_path / text_path / f'{sha}.json'


def _get_bm25Okapi(index_tokens):
    has_tokens = index_tokens.apply(len).sum() > 0
    if not has_tokens:
        index_tokens.loc[0] = ['no', 'tokens']
    return BM25Okapi(index_tokens.tolist())


def lookup_tokens(shas, token_map):
    if not isinstance(shas, str): return []
    for sha in shas.split(';'):
        tokens = token_map.get(sha.strip())
        if tokens:
            return tokens


def _set_index_from_text(metadata, data_dir):
    print('Creating the BM25 index from the text contents of the papers')
    tick = time.time()
    for catalog in JSON_CATALOGS:
        catalog_idx = metadata.full_text_file == catalog
        metadata_papers = metadata.loc[catalog_idx, ['sha']].copy().reset_index()

        # Load the json catalog
        json_papers = load_json_texts(json_dirs=catalog, data_path=data_dir, tokenize=True)

        # Set the index tokens from the json_papers to the metadata
        sha_tokens = metadata_papers.merge(json_papers, how='left', on='sha').set_index('index')

        # Handle records with multiple shas
        has_multiple = (sha_tokens.sha.fillna('').str.contains(';'))
        token_map = json_papers[['sha', 'index_tokens']].set_index('sha').to_dict()['index_tokens']
        sha_tokens.loc[has_multiple, 'index_tokens'] \
            = sha_tokens.loc[has_multiple, 'sha'].apply(lambda sha: lookup_tokens(sha, token_map))

        metadata.loc[catalog_idx, 'index_tokens'] = sha_tokens.index_tokens
        null_tokens = metadata.index_tokens.isnull()
        # Fill null tokens with an empty list
        metadata.loc[null_tokens, 'index_tokens'] = \
            metadata.loc[null_tokens, 'index_tokens'].fillna('').apply(lambda d: d.split(' '))
    tock = time.time()
    print('Finished Indexing texts in', round(tock - tick, 0), 'seconds')
    return metadata


class ResearchPapers:

    def __init__(self, metadata, data_dir='data', index='abstract', view='html'):
        self.data_path = Path(data_dir)
        self.num_results = 10
        self.view = view
        self.metadata = metadata
        if 'index_tokens' not in metadata:
            print('\nIndexing research papers')
            if any([index == t for t in ['text', 'texts', 'content', 'contents']]):
                _set_index_from_text(self.metadata, data_dir)
            else:
                print('Creating the BM25 index from the abstracts of the papers')
                print('Use index="text" if you want to index the texts of the paper instead')
                tick = time.time()
                self.metadata['index_tokens'] = metadata.abstract.apply(preprocess)
                tock = time.time()
                print('Finished Indexing in', round(tock - tick, 0), 'seconds')

        self.bm25 = _get_bm25Okapi(self.metadata.index_tokens)

        if 'antivirals' not in self.metadata:
            # Add antiviral column
            self.metadata['antivirals'] = self.metadata.index_tokens \
                .apply(lambda t:
                       ','.join([token for token in t if token.endswith('vir')]))

    def nlp(self):
        # Topic model
        lda_model, dictionary, corpus = get_lda_model(self.index_tokens, num_topics=8)
        print('Assigning LDA topics')
        topic_vector = self.index_tokens.apply(lambda tokens: get_topic_vector(lda_model, dictionary, tokens))
        self.metadata['topic_vector'] = topic_vector
        self.metadata['top_topic'] = topic_vector.apply(np.argmax)

    def create_document_index(self):
        print('Indexing research papers')
        tick = time.time()
        index_tokens = self._create_index_tokens()
        # Add antiviral column
        self.metadata['antivirals'] = index_tokens.apply(lambda t:
                                                         ','.join([token for token in t if token.endswith('vir')]))
        # Does it have any covid term?
        self.bm25 = BM25Okapi(index_tokens.tolist())
        tock = time.time()
        print('Finished Indexing in', round(tock - tick, 0), 'seconds')

    def get_json_paths(self):
        return self.metadata.apply(lambda d:
                                   np.nan if not d.has_text else get_json_path(self.data_path, d.full_text_file, d.sha),
                                   axis=1)

    def describe(self):
        cols = [col for col in self.metadata if not col in ['sha', 'index_tokens']]
        return describe_dataframe(self.metadata, cols)

    def __getitem__(self, item):
        if isinstance(item, int):
            paper = self.metadata.iloc[item]
        else:
            paper = self.metadata[self.metadata.sha == item]

        return Paper(paper, self.data_path)

    def covid_related(self):
        return self.query('covid_related')

    def not_covid_related(self):
        return self.query('~covid_related')

    def __len__(self):
        return len(self.metadata)

    def _make_copy(self, new_data):
        return ResearchPapers(metadata=new_data.copy(),
                              data_dir=self.data_path,
                              view=self.view)

    def query(self, query):
        data = self.metadata.query(query)
        return self._make_copy(data)

    def after(self, date, include_null_dates=False):
        cond = self.metadata.published >= date
        if include_null_dates:
            cond = cond | self.metadata.published.isnull()
        return self._make_copy(self.metadata[cond])

    def before(self, date, include_null_dates=False):
        cond = self.metadata.published < date
        if include_null_dates:
            cond = cond | self.metadata.published.isnull()
        return self._make_copy(self.metadata[cond])

    def get_papers(self, sub_catalog):
        return self.query(f'full_text_file =="{sub_catalog}"')

    def since_sars(self, include_null_dates=False):
        return self.after(SARS_DATE, include_null_dates)

    def before_sars(self, include_null_dates=False):
        return self.before(SARS_DATE, include_null_dates)

    def since_sarscov2(self, include_null_dates=False):
        return self.after(SARS_COV_2_DATE, include_null_dates)

    def before_sarscov2(self, include_null_dates=False):
        return self.before(SARS_COV_2_DATE, include_null_dates)

    def with_text(self):
        return self.query('has_text')

    def contains(self, search_str, column='abstract'):
        cond = self.metadata[column].fillna('').str.contains(search_str)
        return self._make_copy(self.metadata[cond])

    def match(self, search_str, column='abstract'):
        cond = self.metadata[column].fillna('').str.match(search_str)
        return self._make_copy(self.metadata[cond])

    def head(self, n):
        return self._make_copy(self.metadata.head(n))

    def tail(self, n):
        return self._make_copy(self.metadata.tail(n).copy())

    def sample(self, n):
        return self._make_copy(self.metadata.sample(n).copy())

    def abstracts(self):
        return pd.Series([self.__getitem__(i).abstract() for i in range(len(self))])

    def titles(self):
        return pd.Series([self.__getitem__(i).title() for i in range(len(self))])

    def get_summary(self):
        summary_df = pd.DataFrame({'Papers': [len(self.metadata)],
                           'Oldest': [self.metadata.published.min()],
                           'Newest': [self.metadata.published.max()],
                           'SARS-COV-2': [self.metadata.covid_related.sum()],
                           'SARS': [self.metadata.sars.sum()],
                           'Coronavirus': [self.metadata.coronavirus.sum()],
                           'Virus': [self.metadata.virus.sum()],
                           'Antivirals': [self.metadata.antivirals.apply(lambda a: len(a) > 0).sum()]},
                          index=[''])
        summary_df.Newest = summary_df.Newest.fillna('')
        summary_df.Oldest = summary_df.Oldest.fillna('')
        return summary_df

    def _repr_html_(self):
        display_cols = ['title', 'abstract', 'journal', 'authors', 'published', 'when']
        return render_html('ResearchPapers', summary=self.get_summary()._repr_html_(),
                           research_papers=self.metadata[display_cols]._repr_html_())

    @staticmethod
    def load_metadata(data_path=None):
        if not data_path:
            data_path = find_data_dir()

        print('Loading metadata from', data_path)
        metadata_path = PurePath(data_path) / 'metadata.csv'
        dtypes = {'Microsoft Academic Paper ID': 'str', 'pubmed_id': str}
        renames = {'source_x': 'source', 'has_full_text': 'has_text'}
        metadata = pd.read_csv(metadata_path, dtype=dtypes, parse_dates=['publish_time']).rename(columns=renames)
        # category_dict = {'license': 'category', 'source_x': 'category',
        #                 'journal': 'category', 'full_text_file': 'category'}
        metadata = clean_metadata(metadata)
        return metadata

    @classmethod
    def load(cls, data_dir=None, index=None):
        if data_dir:
            data_path = Path(data_dir) / CORD_CHALLENGE_PATH
        else:
            data_path = find_data_dir()
        metadata = cls.load_metadata(data_path)
        return cls(metadata, data_path, index=index)

    @staticmethod
    def from_pickle(save_dir='data'):
        save_path = PurePath(save_dir) / _RESEARCH_PAPERS_SAVE_FILE
        with open(save_path, 'rb') as f:
            return pickle.load(f)

    def save(self, save_dir='data'):
        save_path = PurePath(save_dir) / _RESEARCH_PAPERS_SAVE_FILE
        print('Saving to', save_path)
        with open(save_path, 'wb') as f:
            pickle.dump(self, f)

    def _create_index_tokens(self):
        abstract_tokens = self.metadata.abstract.apply(preprocess)
        return abstract_tokens

    def search(self, search_string,
               num_results=None,
               covid_related=False,
               start_date=None,
               end_date=None,
               view='html'):
        if not self.bm25:
            print('BM25 index does not exist .. in search.. recreating')
            self.create_document_index()

        n_results = num_results or self.num_results
        search_terms = preprocess(search_string)
        doc_scores = self.bm25.get_scores(search_terms)

        # Get the index from the doc scores
        ind = np.argsort(doc_scores)[::-1]
        results = self.metadata.iloc[ind].copy()
        results['Score'] = doc_scores[ind].round(1)

        # Filter covid related
        if covid_related:
            results = results[results.covid_related]

        # Filter by dates
        if start_date:
            results = results[results.published >= start_date]

        if end_date:
            results = results[results.published < end_date]

        # Only include results over a minimum threshold
        results = results[results.Score > _MINIMUM_SEARCH_SCORE]

        # Show only up to n_results
        results = results.head(n_results)

        # Create the final results
        results = results.reset_index(drop=True)

        # Return Search Results
        return SearchResults(results, self.data_path, view=view)

    def _search_papers(self, output, SearchTerms: str, num_results=None, view=None,
                       start_date=None, end_date=None):
        if len(SearchTerms) < 5:
            return
        search_results = self.search(SearchTerms, num_results=num_results, view=view,
                                     start_date=start_date, end_date=end_date)
        if len(search_results) > 0:
            with output:
                clear_output()
                display(search_results)
        return search_results

    def searchbar(self, initial_search_terms='', num_results=10, view=None):
        text_input = widgets.Text(layout=widgets.Layout(width='400px'), value=initial_search_terms)
        search_dates_slider = SearchDatesSlider()
        search_button = widgets.Button(description='Search', button_style='primary',
                                       layout=widgets.Layout(width='100px'))
        search_box = widgets.HBox(children=[text_input, search_button])

        search_widget = widgets.VBox([search_box, search_dates_slider])

        output = widgets.Output()

        def do_search():
            search_terms = text_input.value.strip()
            if search_terms and len(search_terms) >= 4:
                start_date, end_date = search_dates_slider.value
                self._search_papers(output=output, SearchTerms=search_terms, num_results=num_results, view=view,
                                    start_date=start_date, end_date=end_date)

        def button_search_handler(btn):
            with output:
                clear_output()
            do_search()

        def text_search_handler(change):
            if len(change['new'].split(' ')) != len(change['old'].split(' ')):
                do_search()

        def date_handler(change):
            do_search()

        search_button.on_click(button_search_handler)
        text_input.observe(text_search_handler, names='value')
        search_dates_slider.observe(date_handler, names='value')

        display(search_widget)
        display(output)

        # Show the initial terms
        if initial_search_terms:
            do_search()


def SearchDatesSlider():
    options = [(' 1951 ', '1951-01-01'), (' SARS 2003 ', '2002-11-01'),
               (' H1N1 2009 ', '2009-04-01'), (' COVID 19 ', '2019-11-30'),
               (' 2020 ', '2020-12-31')]
    return widgets.SelectionRangeSlider(
        options=options,
        description='Dates',
        disabled=False,
        value=('2002-11-01', '2020-12-31'),
        layout={'width': '480px'}
    )


class Paper:
    '''
    A single research paper
    '''

    def __init__(self, item, data_path):
        self.sha = item.sha
        self.catalog = item.full_text_file
        self.metadata = item
        self.data_path = data_path

    def get_json_paper(self):
        if self.catalog:
            json_path = self.data_path / self.catalog / self.catalog / f'{self.sha}.json'
            if json_path.exists():
                return load_json_paper(json_path)

    @property
    def doi(self):
        return self.metadata.doi

    @property
    def html(self):
        json_paper = self.get_json_paper()
        if json_paper:
            return json_paper.html

    @property
    def text(self):
        '''
        Load the paper from doi.org and display as text. Requires Internet to be ON
        '''
        json_paper = self.get_json_paper()
        if json_paper:
            return json_paper.text

    @property
    def abstract(self):
        return self.metadata.abstract

    @property
    def summary(self):
        return summarize(self.abstract)

    @property
    def title(self):
        return self.metadata.title

    def has_text(self):
        return self.paper.has_text

    @property
    def authors(self, split=False):
        json_paper = self.get_json_paper()
        if json_paper:
            return ', '.join(json_paper.authors)
        '''
        Get a list of authors
        '''
        authors = self.paper.loc['authors'].values[0]
        if not authors:
            return []
        if not split:
            return authors
        if authors.startswith('['):
            authors = authors.lstrip('[').rstrip(']')
            return [a.strip().replace("\'", "") for a in authors.split("\',")]

        # Todo: Handle cases where author names are separated by ","
        return [a.strip() for a in authors.split(';')]

    def _repr_html_(self):
        paper_meta = self.metadata.to_frame().T
        paper_meta = paper_meta[['published', 'when', 'authors', 'covid_related', 'doi', 'journal']]
        paper_meta.index = ['']

        return render_html('Paper', paper=self, meta=paper_meta)


class SearchResults:

    def __init__(self, data: pd.DataFrame, data_path, view='html'):
        self.data_path = data_path
        self.results = data.dropna(subset=['title'])
        self.results.authors = self.results.authors.apply(str).replace("'", '').replace('[', '').replace(']', '')
        self.results['url'] = self.results.doi.apply(doi_url)
        self.results['summary'] = self.results.abstract.apply(summarize)
        self.columns = [col for col in ['sha', 'title', 'summary', 'when', 'authors'] if col in self.results]
        self.view = view

    def __getitem__(self, item):
        return Paper(self.results.loc[item], self.data_path)

    def __len__(self):
        return len(self.results)

    def _view_html(self, search_results):
        _results = [{'title': rec['title'],
                     'authors': shorten(rec['authors'], 200),
                     'abstract': shorten(rec['abstract'], 300),
                     'summary': shorten(summarize(rec['abstract']), 500),
                     'when': rec['when'],
                     'url': rec['url'],
                     'is_kaggle': is_kaggle()
                     }
                    for rec in search_results.to_dict('records')]
        return render_html('SearchResultsHTML', search_results=_results)

    def _repr_html_(self):
        if self.view == 'html':
            return self._view_html(self.results)
        elif any([self.view == v for v in ['df', 'dataframe', 'table']]):
            display_cols = [col for col in self.columns if not col == 'sha']
            return self.results[display_cols]._repr_html_()
        else:
            return self._view_html(self.results)
