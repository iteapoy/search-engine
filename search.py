import csv
import os
from db import DBManager
import config
import nltk
from multiprocessing import Pool
from timeit import default_timer as timer
import enchant
from fnmatch import fnmatch
from utils import extract_terms_from_sentence, wash_text, format_text, cosine_similarity, split, \
    cal_norm_tf_idf, extarct_id_tf, merge_scores, fast_cosine_similarity, weighted_zone, \
    cal_entropy_tf_f, cal_tf_idf, remove_puntuation
import itertools


def process_term_posting(packed_params, total_pages=4599026, rank_mode=1):
    """
    Calculate normed query tf-idf and doc tf-idf for term

    Args:
        posting (tuple): (term, docs)
        tf_query (list): list of term frequency in query
        total_pages (int): the amount of pages

    Returns:
        normed query tf-idf
        and a dictionary of doc tf-idf {docid: tf-idf}
    """
    if len(packed_params) == 0:
        return [], {}
    if len(packed_params) == 1:
        # print(packed_params)
        postings, tf_query = packed_params[0][0], packed_params[0][1]
        postings = [postings]
        tf_query = [tf_query]
        rank_mode = [packed_params[0][2]]
        # print(postings)
    else:
        postings, tf_query, rank_mode = packed_params
    # print(postings)
    rank_mode = rank_mode[0]
    # print("here:", rank_mode)
    n_unique_terms = len(postings)
    query_vec = [0]*n_unique_terms

    doc_vecs = {}
    terms = []
    for i, (term, docs) in enumerate(postings):
        terms.append(term)
        if len(docs) > 0:
            docid_tf = extarct_id_tf(docs)

            if rank_mode == 3:  # use entropy to calculate idf
                print("Using engopy + cosine to rank")
                df = cal_entropy_tf_f(docid_tf, total_pages)
            else:
                df = len(docid_tf)

            if rank_mode == 1:  # use unnormalized tf-idf
                print("Using unnormalized tf-idf to rank")
                query_vec[i] = cal_tf_idf(
                    tf=tf_query[i], df=df, N_doc=total_pages)
            else:
                query_vec[i] = cal_norm_tf_idf(
                    tf=tf_query[i], df=df, N_doc=total_pages)

            # sort by tf
            docid_tf = sorted(docid_tf, key=lambda d: d[1], reverse=True)
            for doc_id, tf in docid_tf[:config.max_docs_per_term]:
                if doc_id not in doc_vecs.keys():
                    doc_vecs[doc_id] = [0] * n_unique_terms
                if rank_mode == 1:
                    doc_vecs[doc_id][i] = cal_tf_idf(tf, df, total_pages)
                    #print("compare:", cal_tf_idf(tf, df, total_pages), cal_norm_tf_idf(tf, df, total_pages))
                else:
                    doc_vecs[doc_id][i] = cal_norm_tf_idf(tf, df, total_pages)

    return query_vec, doc_vecs, terms


def select_term_given_vec(vec, terms):
    select = []
    for i in range(len(vec)):
        if vec[i] != 0:
            select.append(terms[i])
    # print(vec, terms, select)
    return select


class SearchManager:
    """
    Vocab size: 5405638
    Total Docs: 4599026
    """
    def __init__(self):

        self.db = DBManager(page_db=config.demo_page_db,
                            index_db=config.demo_index_db)
        # self.db = DBManager(page_db=config.page_db, index_db=config.index_db)
        # self.total_pages = self.db.get_current_max_page_id()+1
        self.word_freq = read_freq_word()
        self.common_words = self._init_common_word()
        self.recommender = self._init_recommender()
        self.page_buffer = None
        self.querys = None  # 用于detail页面的高亮

    def _init_common_word(self):
        with open('data/google-20k.txt', 'r') as fd:
            common_words = fd.readlines()
        common_words = [w[:-1] for w in common_words]
        return common_words

    def _init_recommender(self, additional_words=[]):
        recommender = enchant.Dict("en_US")
        for w in additional_words:
            recommender.add_to_session(w)

        return recommender

    def _search(self, query, rank_mode, concurrent=True):
        terms = extract_terms_from_sentence(query)
        if len(terms) == 0:
            return None, None

        unique_terms = set(terms)
        n_unique = len(unique_terms)
        if n_unique < 2:
            print(f'Warning: too few valid search terms. (only {n_unique})')

        start = timer()
        postings = self.db.read_postings(unique_terms)
        print(f'read postings in {timer()-start} s')
        # exit()

        # 1. calculate tf-idf for query and docs
        tf_query = [terms.count(t[0]) for t in postings]
        print(len(postings))
        n_unique = len(postings)
        if n_unique == 0:
            return None, None
            
        print("in _search", rank_mode)
        if not concurrent or n_unique < 2:
            start = timer()
            query_vec, doc_vecs, valid_terms = process_term_posting(
                (postings, tf_query, [rank_mode]*len(postings)))
            print(f"Sequential process cost {timer()-start} s")
        # exit()
        else:
            start = timer()
            n_proc = min(n_unique, os.cpu_count() // 2)
            pool = Pool(n_proc)
            n_rank_mode = [rank_mode] * len(postings)
            pack = list(zip(postings, tf_query, n_rank_mode))
            # print(len(pack))
            n_part_params = split(pack, n_proc)
            # print(n_part_params)
            results = pool.map(process_term_posting, n_part_params)
            pool.close()
            pool.join()
            query_vec_list, doc_vecs_list, term_list = zip(*results)
            valid_terms = list(itertools.chain(*term_list))
            query_vec, doc_vecs = merge_scores(query_vec_list, doc_vecs_list,
                                               n_unique)
            print(f"Concurrent process cost {timer()-start} s")
        # 2. compute query-doc similarity
        doc_scores = []  # (doc_id, score)

        for doc_id, doc_vec in doc_vecs.items():
            # calculate cosine similarity
            doc_scores.append((doc_id, cosine_similarity(
                query_vec, doc_vec), select_term_given_vec(doc_vec, valid_terms),
                sum(doc_vec)))
        # sort
        doc_scores = sorted(doc_scores, key=lambda d: d[1], reverse=True)

        print(f'Searched for {len(doc_scores)} pages')

        # return doc_scores[:config.max_return_docs]
        return doc_scores, set(valid_terms)

    def most_frequent(self, candidates):
        ret = (candidates[0], -1)
        for s in candidates:
            if s not in self.word_freq.keys():
                continue
            if int(self.word_freq[s]) > ret[1]:
                ret = (s, int(self.word_freq[s]))
        return ret[0]

    def fuzzy_query(self, query):
        # print(common_words.__)
        new_query = query[:]
        for t in query.split():
            exist = self.recommender.check(t)
            # replace the non-exist word by the most similiar and frequently used one
            if not exist:
                suggest = self.recommender.suggest(t)[:5]
                if len(suggest) > 0:
                    # print(suggest)
                    pick = self.most_frequent(suggest)
                    new_query = new_query.replace(t, pick)

        if query != new_query:
            print(f'Do you mean \'{new_query}\'?')

        # self.search(new_query)
        return new_query

    def wildcard_query(self, query):
        """
        Support *

        Example:
            he*o -> hello, hero, ...
        """

        new_query = query[:]
        for t in query.split():
            if '*' in t:
                for word in self.common_words:
                    if fnmatch(word, t):
                        new_query = new_query.replace(t, word)
                        break

        if query != new_query:
            print(f'Search for \'{new_query}\'?')

        # self.search(new_query)
        return new_query

    def search(self, query, rank_mode=2, concurrent=True):
        """
        wrapper of search logic

        Args:
            query (string): the input query string
        """
        print(f"Searching for \'{query}\'")

        start = timer()
        query = wash_text(remove_puntuation(query))
        # print(f'washed: {query}')
        if '*' in query:
            query = self.wildcard_query(query)

        fuzzy_query = self.fuzzy_query(query)

        querys = [query, fuzzy_query]
        if query == fuzzy_query:
            querys[1] = None

        # exit()

        # pages, doc_scores = self._search(query, concurrent=True)
        print("in search", rank_mode)
        doc_scores, unique_terms = self._search(
            query, concurrent=concurrent, rank_mode=rank_mode)  # (id, score, terms)
        if doc_scores is None:
            print(f'No valid input in {query}')
            # return 0
            return None, '', querys, 0

        if fuzzy_query != query:
            doc_scores_fuzzy, unique_terms_fuzzy = self._search(fuzzy_query, concurrent=concurrent, rank_mode=rank_mode)
            if doc_scores_fuzzy is not None:
                unique_terms = set.union(unique_terms, unique_terms_fuzzy)
                doc_scores.extend(doc_scores_fuzzy)
                doc_scores = sorted(doc_scores,
                                    key=lambda d: d[1],
                                    reverse=True)

        n_searched = len(doc_scores)

        # remove repeated pages
        unique_ids = set()
        unique_doc_scores = []
        for d in doc_scores:
            if d[0] not in unique_ids:
                unique_doc_scores.append(d)
            if len(unique_doc_scores) == config.max_return_docs_firststep:
                break

        # sync pages and doc_scores
        page_ids = [d[0] for d in unique_doc_scores]
        read_start = timer()
        pages = self.db.read_pages(page_ids)
        print(f"Load pages cost {timer()-read_start} s")

        sync_doc_scores = []
        sync_page = []
        for page in pages:
            for d in unique_doc_scores:
                if d[0] == page[0]:
                    # print((d[0], page[0]))
                    sync_doc_scores.append(d)
                    sync_page.append(page)
                    break

        doc_scores = sync_doc_scores
        pages = sync_page
        score_page = []
        for score, page in zip(doc_scores, pages):
            score_page.append([score, page])
        score_page = sorted(score_page, key=lambda d: d[0][1], reverse=True)
        doc_scores = [score[0] for score in score_page]
        pages = [page[1] for page in score_page]

        if rank_mode == 4:
            print("Using fast cosine to rank")
            doc_fast_cos_scores = []
            for doc_score, page in zip(doc_scores, pages):
                doc_fast_cos_scores.append((doc_score[0],
                                            fast_cosine_similarity(doc_score, page), doc_score[2], page))

            doc_scores = sorted(doc_fast_cos_scores, key=lambda d: d[1], reverse=True)
            page_ids = [d[0] for d in doc_scores[:config.max_return_docs]]
            pages = [d[3] for d in doc_scores[:config.max_return_docs]]
            #print("after", len(pages))

        elif rank_mode == 5:  # weighted zone
            print("Using weighted zone to rank")
            # After sorting the top k documents, using weighted zone ranking to rerank the documents
            doc_weighted_scores = []
            for doc_score, page in zip(doc_scores, pages):
                doc_weighted_scores.append((doc_score[0], weighted_zone(unique_terms, page, config.w_title, config.w_body), doc_score[2], page))

            doc_scores = sorted(doc_weighted_scores, key=lambda d: d[1], reverse=True)
            page_ids = [d[0] for d in doc_scores[:config.max_return_docs]]
            pages = [d[3] for d in doc_scores[:config.max_return_docs]]
        else:
            page_ids = page_ids[:config.max_return_docs]
            pages = pages[:config.max_return_docs]
        #print("pages:", pages)
        # return

        time_cost = timer()-start

        # print("pages", pages)
        # print("score", doc_scores)
        page_list = [{
            'ID': page[0],
            'title':page[1],
            'content': format_text(page[2]),
            'score': '{:.4f}'.format(doc_scores[i][1]),
            'terms': doc_scores[i][2]
        } for i, page in enumerate(pages)]

        self.page_buffer = page_list

        time_str = '{:.3f}'.format(time_cost)

        # return time_cost

        # for page in page_list:
        #     # print("ID: {}\tScores: {}\tTerms:{}".format(
        #     #     page['ID'], page['score'], page['terms']))
        #     to_remove = []
        #     for t in page['terms']:
        #         if t not in page['content'].lower():
        #             print(f"no {t} in page {page['ID']}")
        #             # page['terms'].remove(t)
        #             to_remove.append(t)
        #     for t in to_remove:
        #         page['terms'].remove(t)

        # print(time_str, querys, n_searched)
        #print("top k", len(page_list), n_searched)

        return page_list, time_str, querys, n_searched

    def read_page(self, page_id):
        for page in self.page_buffer:
            if page['ID'] == page_id:

                return page

        raise NotImplementedError(page_id)


def read_freq_word():
    freq_dic = {}
    with open('data/unigram_freq.csv', 'rt') as f:
        cr = csv.reader(f)
        for i, row in enumerate(cr):
            if i == 0:
                continue
            freq_dic[row[0]] = row[1]
    return freq_dic


if __name__ == "__main__":
    proc = SearchManager()

    query = 'tradition draw example'
    # query = input('Please input query:\n')
    # mode 1: tf-idf
    # mode 2: cosine sim
    # mode 3: entropy
    # mode 4: fast cosine
    # mode 5: weighted zone

    proc.search(query, rank_mode = 5)
