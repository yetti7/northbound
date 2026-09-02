from dataclasses import dataclass

from .hardcover import HardcoverConnectionError, execute_graphql

LIBRARY_STATE_QUERY = '''
query NorthboundLibraryState($bookId: Int!) {
  me { user_books(where: {book_id: {_eq: $bookId}}, limit: 2) { id book_id edition_id status_id user_book_status { id slug } } }
  user_book_statuses(where: {slug: {_eq: "read"}}, limit: 2) { id slug }
}
'''
CREATE_USER_BOOK_MUTATION = '''
mutation NorthboundCreateReadBook($object: UserBookCreateInput!) {
  insert_user_book(object: $object) { id error user_book { id book_id edition_id status_id user_book_status { id slug } } }
}
'''
UPDATE_USER_BOOK_MUTATION = '''
mutation NorthboundUpdateReadBook($id: Int!, $object: UserBookUpdateInput!) {
  update_user_book(id: $id, object: $object) { id error user_book { id book_id edition_id status_id user_book_status { id slug } } }
}
'''
READ_OCCURRENCE_QUERY = '''
query NorthboundReadOccurrence($id: Int!) {
  user_book_reads_by_pk(id: $id) { id user_book_id edition_id started_at finished_at finished_at_precision }
}
'''
CREATE_READ_OCCURRENCE_MUTATION = '''
mutation NorthboundCreateReadOccurrence($userBookId: Int!, $read: DatesReadInput!) {
  insert_user_book_read(user_book_id: $userBookId, user_book_read: $read) {
    id error user_book_read { id user_book_id edition_id started_at finished_at finished_at_precision }
  }
}
'''
UPDATE_READ_OCCURRENCE_MUTATION = '''
mutation NorthboundUpdateReadOccurrence($id: Int!, $read: DatesReadInput!) {
  update_user_book_read(id: $id, object: $read) {
    id error user_book_read { id user_book_id edition_id started_at finished_at finished_at_precision }
  }
}
'''


@dataclass(frozen=True, slots=True)
class LibraryState:
    user_book: dict | None
    read_status_id: int


def _one_reader(me):
    if isinstance(me, list):
        if len(me) != 1 or not isinstance(me[0], dict):
            raise HardcoverConnectionError("Hardcover did not return one Reader library.")
        return me[0]
    if isinstance(me, dict):
        return me
    raise HardcoverConnectionError("Hardcover did not return the Reader library.")


def _identifier(value):
    if isinstance(value, bool) or not str(value).isdigit() or int(value) <= 0:
        raise HardcoverConnectionError("Hardcover did not confirm a valid library identifier.", classification="unreadable_response", retryable=True)
    return str(value)


def read_library_state(token, book_id):
    data = execute_graphql(token, LIBRARY_STATE_QUERY, {"bookId": int(book_id)})
    statuses = data.get("user_book_statuses") or []
    if not isinstance(statuses, list) or len(statuses) != 1 or not isinstance(statuses[0], dict) or statuses[0].get("slug") != "read":
        raise HardcoverConnectionError("Hardcover did not identify one read status.")
    user_books = _one_reader(data.get("me")).get("user_books")
    if not isinstance(user_books, list):
        raise HardcoverConnectionError("Hardcover did not return a complete library result.", classification="unreadable_response", retryable=True)
    if len(user_books) > 1:
        raise HardcoverConnectionError("Hardcover returned multiple library records for the matched book.")
    if user_books:
        if not isinstance(user_books[0], dict):
            raise HardcoverConnectionError("Hardcover returned an unexpected library record.")
        _identifier(user_books[0].get("id"))
        if "book_id" in user_books[0] and str(user_books[0]["book_id"]) != str(book_id):
            raise HardcoverConnectionError("Hardcover returned a different library book.")
    return LibraryState(user_book=user_books[0] if user_books else None, read_status_id=int(_identifier(statuses[0].get("id"))))


def _mutation_result(data, field):
    result = data.get(field) or {}
    if not isinstance(result, dict):
        raise HardcoverConnectionError("Hardcover returned an unexpected update result.", classification="unreadable_response", retryable=True)
    if result.get("error"):
        raise HardcoverConnectionError("Hardcover rejected the library update.")
    nested = result.get("user_book") or result.get("user_book_read") or {}
    identifier = result.get("id") or (nested.get("id") if isinstance(nested, dict) else None)
    if identifier in (None, ""):
        raise HardcoverConnectionError("Hardcover did not confirm the library update.", classification="unreadable_response", retryable=True)
    return _identifier(identifier)


def create_read_user_book(token, *, book_id, read_status_id, edition_id=None):
    obj = {"book_id": int(book_id), "status_id": int(read_status_id)}
    if edition_id is not None:
        obj["edition_id"] = int(edition_id)
    return _mutation_result(execute_graphql(token, CREATE_USER_BOOK_MUTATION, {"object": obj}), "insert_user_book")


def update_user_book_to_read(token, *, user_book_id, read_status_id):
    variables = {"id": int(user_book_id), "object": {"status_id": int(read_status_id)}}
    return _mutation_result(execute_graphql(token, UPDATE_USER_BOOK_MUTATION, variables), "update_user_book")


def read_occurrence(token, read_id):
    return execute_graphql(token, READ_OCCURRENCE_QUERY, {"id": int(read_id)}).get("user_book_reads_by_pk")


def create_read_occurrence(token, *, user_book_id, finished_at, edition_id=None):
    read = {"finished_at": finished_at.isoformat()}
    if edition_id is not None:
        read["edition_id"] = int(edition_id)
    data = execute_graphql(token, CREATE_READ_OCCURRENCE_MUTATION, {"userBookId": int(user_book_id), "read": read})
    return _mutation_result(data, "insert_user_book_read")


def update_read_occurrence(token, *, read_id, finished_at):
    variables = {"id": int(read_id), "read": {"finished_at": finished_at.isoformat()}}
    return _mutation_result(execute_graphql(token, UPDATE_READ_OCCURRENCE_MUTATION, variables), "update_user_book_read")
