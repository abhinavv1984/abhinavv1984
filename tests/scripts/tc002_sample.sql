-- Sample SQL returning zero rows (assuming sys.objects has names)
SELECT TOP 0 * FROM sys.objects WHERE name LIKE 'no_such_object_%';
