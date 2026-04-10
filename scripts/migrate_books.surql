-- Migration: fix linked flag for all book-linked memories
UPDATE memory SET linked = true
WHERE id IN (SELECT VALUE in FROM relates WHERE type = 'from_book')
AND linked = false;
