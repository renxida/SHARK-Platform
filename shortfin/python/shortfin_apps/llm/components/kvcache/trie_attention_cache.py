from typing import Dict, Set, List, Tuple, Optional
from dataclasses import dataclass
import time
import math
import heapq
from .page_pool import PagePool, PageInfo
from .base_attention_cache import (
    BasePagedAttentionCache,
    PageAllocation,
    CacheAllocationFailure,
)


@dataclass
class TrieNode:
    """Node of the block trie for paged attention cache.

    Each node represents a page of tokens in the cache, with edges representing
    token sequences that can follow. This allows prefix sharing between sequences
    that have common prefixes.

    Attributes:
        tokens: Tuple of tokens stored in this node's page
        page: PageInfo object containing the actual cache page
        children: Dict mapping token sequences to child nodes
        parent: Parent node in the trie (None for root)
        ref_count: Number of active references to this node
        access_time: Last access timestamp for LRU eviction
    """

    tokens: Tuple[int, ...]
    page: PageInfo
    children: Optional[Dict[Tuple[int, ...], "TrieNode"]] = None
    parent: Optional["TrieNode"] = None
    ref_count: int = 0
    access_time: float = 0.0

    def __post_init__(self) -> None:
        """Initialize children dict and access time if not provided."""
        if self.children is None:
            self.children = {}
        self.access_time = time.monotonic()

    def create_child(self, tokens: Tuple[int, ...], page: PageInfo) -> "TrieNode":
        """Create a new child node with the given tokens and page.

        Args:
            tokens: Sequence of tokens for the new node
            page: PageInfo for the new node's cache page

        Returns:
            The newly created child node
        """
        new_node = TrieNode(tokens=tokens, page=page, parent=self)
        self.children[tokens] = new_node
        return new_node

    def unlink(self) -> None:
        """Remove this node from its parent's children."""
        if self.parent is not None:
            del self.parent.children[self.tokens]
            self.parent = None

    def __hash__(self) -> int:
        """Nodes are uniquely identified by their memory address."""
        return id(self)

    def __eq__(self, other: object) -> bool:
        """Nodes are equal only if they are the same object."""
        return self is other


class TriePageAttentionCacheAllocation(PageAllocation):
    """Represents a page allocation in the trie-based cache.

    Tracks both previously cached pages and newly allocated pages,
    implementing the PageAllocation protocol for the trie cache.

    Attributes:
        cache: The parent cache this allocation belongs to
        tokens: Complete sequence of tokens this allocation represents
        last_cached_node: Last matched node in the trie
        cached_pages: List of pages already in cache
        newly_acquired_pages: List of newly allocated pages
        start_index: Index where cached tokens end and new tokens begin
    """

    def __init__(
        self,
        cache: "TriePagedAttentionCache",
        tokens: List[int],
        last_cached_node: TrieNode,
        cached_pages: List[PageInfo],
        newly_acquired_pages: List[PageInfo],
        start_index: int,
    ):
        self.cache = cache
        self.tokens = tokens
        self.last_cached_node = last_cached_node
        self.cached_pages = cached_pages
        self.newly_acquired_pages = newly_acquired_pages
        self.start_index = start_index
        self._is_released = False

    @property
    def pages(self) -> List[PageInfo]:
        """List all pages in this allocation, both cached and new.

        Returns:
            Combined list of cached and newly acquired pages
        """
        return self.cached_pages + self.newly_acquired_pages

    def publish_pages(self, up_to_page_index: int) -> None:
        """Make pages available in the cache up to the specified index.

        Args:
            up_to_page_index: Number of pages to publish, starting from the beginning
        """
        tokens_per_page = self.cache.tokens_per_page

        publish_token_count = min(len(self.tokens), up_to_page_index * tokens_per_page)

        cur_node = self.last_cached_node
        first_uncached_page_index = len(self.cached_pages)

        uncached_tokens = [
            tuple(self.tokens[i : i + tokens_per_page])
            for i in range(
                first_uncached_page_index * tokens_per_page,
                publish_token_count,
                tokens_per_page,
            )
        ]

        uncached_pages = self.newly_acquired_pages[: len(uncached_tokens)]

        for token_block, page in zip(uncached_tokens, uncached_pages):
            new_node = cur_node.create_child(token_block, page)
            cur_node = new_node

        self.cached_pages.extend(uncached_pages)
        self.newly_acquired_pages = self.newly_acquired_pages[len(uncached_pages) :]

        if cur_node is not self.cache.root:
            self.cache.leaves.add(cur_node)

        cur_node.ref_count += 1
        self.last_cached_node.ref_count -= 1
        self.last_cached_node = cur_node

    def release_pages(self) -> None:
        """Release the allocation's reference to its pages.

        Decrements reference count of the last cached node. When count
        reaches zero, the node becomes eligible for eviction.
        """
        if self._is_released:
            return

        self.last_cached_node.ref_count -= 1
        self._is_released = True


class TriePagedAttentionCache(BasePagedAttentionCache):
    """Trie-based paged attention cache implementation.

    Implements prefix sharing through a trie structure where each node
    represents a page of tokens. Common prefixes between sequences share
    the same nodes/pages, reducing memory usage.

    Attributes:
        root: Root node of the trie
        leaves: Set of leaf nodes for efficient eviction
        page_pool: Pool providing page allocations
        tokens_per_page: Number of tokens that fit in each page
    """

    def __init__(self, page_pool: PagePool, tokens_per_page: int):
        """Initialize the trie cache.

        Args:
            page_pool: Pool to allocate pages from
            tokens_per_page: Number of tokens per page

        Raises:
            ValueError: If tokens_per_page <= 0
        """
        if tokens_per_page <= 0:
            raise ValueError("tokens_per_page must be positive")

        super().__init__(page_pool, tokens_per_page)

        # Create root node with dummy page
        dummy_page = PageInfo(
            index=0,  # Root uses reserved index 0
            pool=self.page_pool,
            token_offset=0,
            token_count=0,
        )
        self.root = TrieNode(tokens=tuple(), page=dummy_page)
        self.leaves: Set[TrieNode] = set()

    def _match(self, tokens: List[int]) -> Tuple[TrieNode, List[PageInfo]]:
        """
        Find the longest prefix match in the trie.

        Walks the trie following the token sequence as far as possible,
        collecting matched pages along the way.

        Args:
            tokens: Sequence of tokens to match

        Returns:
            Tuple of (last matched node, list of matched pages)
        """
        tokens = tuple(tokens)
        matched_pages = []
        cur = self.root

        for i in range(0, len(tokens), self.tokens_per_page):
            token_block = tokens[i : i + self.tokens_per_page]

            if token_block not in cur.children:
                break
            cur = cur.children[token_block]
            cur.access_time = time.monotonic()
            matched_pages.append(cur.page)

        return cur, matched_pages

    def acquire_pages_for_tokens(
        self,
        tokens: List[int],
        extra_token_slots: int = 0,
    ) -> PageAllocation:
        """Acquire pages for a sequence of tokens.

        Attempts to reuse existing cached pages where possible through
        prefix matching, allocating new pages only for the uncached suffix.

        Args:
            tokens: Sequence of tokens needing pages
            extra_token_slots: Additional token slots to allocate beyond tokens

        Returns:
            PageAllocation containing both cached and newly allocated pages

        Raises:
            CacheAllocationFailure: If unable to allocate required pages
        """
        tokens = tuple(tokens)

        cur_node, matched_pages = self._match(tokens)
        cur_node.ref_count += 1

        n_cached_tokens = len(matched_pages) * self.tokens_per_page
        remaining_length = len(tokens) - n_cached_tokens + extra_token_slots
        n_empty_pages = math.ceil(remaining_length / self.tokens_per_page)

        new_pages = self.page_pool.acquire_free_pages(n_empty_pages)

        if new_pages is not None:
            return TriePageAttentionCacheAllocation(
                cache=self,
                tokens=tokens,
                last_cached_node=cur_node,
                cached_pages=matched_pages,
                newly_acquired_pages=new_pages,
                start_index=n_cached_tokens,
            )

        # Try eviction
        self._evict_pages(n_empty_pages - len(self.page_pool.available_pages))
        new_pages = self.page_pool.acquire_free_pages(n_empty_pages)

        if new_pages is None:
            raise CacheAllocationFailure(
                "Failed to acquire pages even after attempting eviction from LRU leaves"
            )

        return TriePageAttentionCacheAllocation(
            cache=self,
            tokens=tokens,
            last_cached_node=cur_node,
            cached_pages=matched_pages,
            newly_acquired_pages=new_pages,
            start_index=n_cached_tokens,
        )

    def _evict_pages(self, max_pages: int) -> int:
        """Evict up to max_pages pages using LRU strategy.

        Evicts from unreferenced leaf nodes first, working up the trie
        as nodes become childless.

        Args:
            max_pages: Maximum number of pages to evict

        Returns:
            Number of pages actually evicted
        """
        pages_to_evict = []

        # Initialize heap with unreferenced leaves
        unused_leaf_heap = [
            (leaf.access_time, leaf) for leaf in self.leaves if leaf.ref_count == 0
        ]
        heapq.heapify(unused_leaf_heap)

        # Evict least recently used nodes
        while unused_leaf_heap and len(pages_to_evict) < max_pages:
            _, leaf = heapq.heappop(unused_leaf_heap)
            pages_to_evict.append(leaf.page)
            parent = leaf.parent
            leaf.unlink()
            self.leaves.remove(leaf)

            # If parent becomes childless, it becomes a leaf
            if parent is not self.root and not parent.children:
                self.leaves.add(parent)
                if parent.ref_count == 0:
                    heapq.heappush(unused_leaf_heap, (parent.access_time, parent))

        if pages_to_evict:
            self.page_pool.free_pages(pages_to_evict)

        return len(pages_to_evict)
