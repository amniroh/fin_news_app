import 'package:flutter/material.dart';
import '../services/api_service.dart';
import '../services/user_service.dart';
import 'learning_modules_screen.dart';
import 'portfolio_simulation_screen.dart';
import 'chat_screen.dart';
import 'progress_screen.dart';

class HomeScreen extends StatefulWidget {
  final Map<String, dynamic>? suggestion;
  
  const HomeScreen({super.key, this.suggestion});

  @override
  State<HomeScreen> createState() => _HomeScreenState();
}

class _HomeScreenState extends State<HomeScreen> {
  int _currentIndex = 0;
  List<Map<String, dynamic>> _feedItems = [];
  bool _isLoading = true;
  Map<String, dynamic>? _userProgress;

  @override
  void initState() {
    super.initState();
    _loadFeed();
    _loadProgress();
  }

  Future<void> _loadFeed() async {
    try {
      final userId = await UserService.getUserId();
      if (userId != null) {
        final response = await ApiService.getFeedItems(userId, limit: 10);
        setState(() {
          _feedItems = List<Map<String, dynamic>>.from(response['items'] ?? []);
          _isLoading = false;
        });
      }
    } catch (e) {
      setState(() => _isLoading = false);
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text('Error loading feed: $e')),
        );
      }
    }
  }

  Future<void> _loadProgress() async {
    try {
      final userId = await UserService.getUserId();
      if (userId != null) {
        final progress = await ApiService.getUserProgress(userId);
        setState(() => _userProgress = progress);
      }
    } catch (e) {
      // Silently fail - progress is optional
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: _buildCurrentScreen(),
      bottomNavigationBar: BottomNavigationBar(
        currentIndex: _currentIndex,
        onTap: (index) => setState(() => _currentIndex = index),
        type: BottomNavigationBarType.fixed,
        items: const [
          BottomNavigationBarItem(
            icon: Icon(Icons.home),
            label: 'Home',
          ),
          BottomNavigationBarItem(
            icon: Icon(Icons.school),
            label: 'Learn',
          ),
          BottomNavigationBarItem(
            icon: Icon(Icons.trending_up),
            label: 'Simulate',
          ),
          BottomNavigationBarItem(
            icon: Icon(Icons.chat),
            label: 'Ask',
          ),
          BottomNavigationBarItem(
            icon: Icon(Icons.emoji_events),
            label: 'Progress',
          ),
        ],
      ),
    );
  }

  Widget _buildCurrentScreen() {
    switch (_currentIndex) {
      case 0:
        return _buildFeedScreen();
      case 1:
        return const LearningModulesScreen();
      case 2:
        return const PortfolioSimulationScreen();
      case 3:
        return const ChatScreen();
      case 4:
        return const ProgressScreen();
      default:
        return _buildFeedScreen();
    }
  }

  Widget _buildFeedScreen() {
    return RefreshIndicator(
      onRefresh: _loadFeed,
      child: CustomScrollView(
        slivers: [
          SliverAppBar(
            expandedHeight: _userProgress != null ? 160 : 100,
            floating: false,
            pinned: true,
            backgroundColor: Colors.blue[700],
            flexibleSpace: FlexibleSpaceBar(
              title: _userProgress == null
                  ? const Text('Your Investment Feed')
                  : null,
              titlePadding: _userProgress == null
                  ? const EdgeInsets.only(left: 16, bottom: 16)
                  : EdgeInsets.zero,
              background: Container(
                decoration: BoxDecoration(
                  gradient: LinearGradient(
                    begin: Alignment.topLeft,
                    end: Alignment.bottomRight,
                    colors: [Colors.blue[400]!, Colors.blue[700]!],
                  ),
                ),
                child: _userProgress != null
                    ? SafeArea(
                        child: Padding(
                          padding: const EdgeInsets.only(top: 8, left: 16, right: 16, bottom: 8),
                          child: Column(
                            mainAxisAlignment: MainAxisAlignment.start,
                            crossAxisAlignment: CrossAxisAlignment.start,
                            children: [
                              const Padding(
                                padding: EdgeInsets.only(bottom: 8),
                                child: Text(
                                  'Your Investment Feed',
                                  style: TextStyle(
                                    color: Colors.white,
                                    fontSize: 20,
                                    fontWeight: FontWeight.bold,
                                  ),
                                ),
                              ),
                              Row(
                                mainAxisAlignment: MainAxisAlignment.spaceAround,
                                crossAxisAlignment: CrossAxisAlignment.start,
                                children: [
                                  Flexible(
                                    flex: 1,
                                    child: _buildStatCard(
                                      'Modules',
                                      '${_userProgress!['completed_modules'] ?? 0}',
                                      Icons.school,
                                    ),
                                  ),
                                  Flexible(
                                    flex: 1,
                                    child: _buildStatCard(
                                      'Streak',
                                      '${_userProgress!['learning_streak'] ?? 0}',
                                      Icons.local_fire_department,
                                    ),
                                  ),
                                  Flexible(
                                    flex: 1,
                                    child: _buildStatCard(
                                      'Badges',
                                      '${(_userProgress!['badges_earned'] as List?)?.length ?? 0}',
                                      Icons.emoji_events,
                                    ),
                                  ),
                                ],
                              ),
                            ],
                          ),
                        ),
                      )
                    : null,
              ),
            ),
          ),
          if (widget.suggestion != null)
            SliverToBoxAdapter(
              child: _buildSuggestionCard(widget.suggestion!),
            ),
          if (_isLoading)
            const SliverFillRemaining(
              child: Center(child: CircularProgressIndicator()),
            )
          else if (_feedItems.isEmpty)
            SliverFillRemaining(
              child: Center(
                child: Column(
                  mainAxisAlignment: MainAxisAlignment.center,
                  children: [
                    Icon(Icons.feed, size: 64, color: Colors.grey[400]),
                    const SizedBox(height: 16),
                    Text(
                      'No feed items yet',
                      style: TextStyle(color: Colors.grey[600]),
                    ),
                  ],
                ),
              ),
            )
          else
            SliverList(
              delegate: SliverChildBuilderDelegate(
                (context, index) => _buildFeedItem(_feedItems[index]),
                childCount: _feedItems.length,
              ),
            ),
        ],
      ),
    );
  }

  Widget _buildStatCard(String label, String value, IconData icon) {
    return Column(
      mainAxisSize: MainAxisSize.min,
      children: [
        Icon(icon, color: Colors.white, size: 20),
        const SizedBox(height: 4),
        Text(
          value,
          style: const TextStyle(
            color: Colors.white,
            fontSize: 16,
            fontWeight: FontWeight.bold,
          ),
          textAlign: TextAlign.center,
          overflow: TextOverflow.ellipsis,
        ),
        Text(
          label,
          style: TextStyle(
            color: Colors.white.withOpacity(0.9),
            fontSize: 11,
          ),
          textAlign: TextAlign.center,
          overflow: TextOverflow.ellipsis,
          maxLines: 1,
        ),
      ],
    );
  }

  Widget _buildSuggestionCard(Map<String, dynamic> suggestion) {
    return Card(
      margin: const EdgeInsets.all(16),
      color: Colors.blue[50],
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          mainAxisSize: MainAxisSize.min,
          children: [
            Row(
              children: [
                Icon(Icons.lightbulb, color: Colors.blue[700]),
                const SizedBox(width: 8),
                Expanded(
                  child: Text(
                    'Your Personalized Plan',
                    style: TextStyle(
                      fontSize: 18,
                      fontWeight: FontWeight.bold,
                      color: Colors.blue[900],
                    ),
                  ),
                ),
              ],
            ),
            const SizedBox(height: 12),
            if (suggestion['suggested_monthly_investment'] != null)
              Text(
                'Suggested monthly investment: \$${suggestion['suggested_monthly_investment']}',
                style: const TextStyle(fontSize: 16),
              ),
            if (suggestion['explanation'] != null) ...[
              const SizedBox(height: 8),
              Text(
                suggestion['explanation'],
                style: const TextStyle(fontSize: 14),
              ),
            ],
          ],
        ),
      ),
    );
  }

  Widget _buildFeedItem(Map<String, dynamic> item) {
    final type = item['type'] ?? 'general';
    final title = item['title'] ?? 'Update';
    final content = item['content'] ?? '';
    final tone = item['tone'] ?? 'neutral';

    Color? cardColor;
    IconData icon;
    
    switch (type) {
      case 'market_update':
        cardColor = tone == 'positive' ? Colors.green[50] : Colors.blue[50];
        icon = Icons.trending_up;
        break;
      case 'concept':
        cardColor = Colors.purple[50];
        icon = Icons.lightbulb;
        break;
      case 'mistake':
        cardColor = Colors.orange[50];
        icon = Icons.warning;
        break;
      case 'psychology':
        cardColor = Colors.pink[50];
        icon = Icons.psychology;
        break;
      default:
        cardColor = Colors.grey[50];
        icon = Icons.info;
    }

    return Card(
      margin: const EdgeInsets.symmetric(horizontal: 16, vertical: 8),
      color: cardColor,
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          mainAxisSize: MainAxisSize.min,
          children: [
            Row(
              children: [
                Icon(icon, color: Colors.blue[700]),
                const SizedBox(width: 8),
                Expanded(
                  child: Text(
                    title,
                    style: const TextStyle(
                      fontSize: 18,
                      fontWeight: FontWeight.bold,
                    ),
                  ),
                ),
              ],
            ),
            const SizedBox(height: 12),
            Text(
              content,
              style: const TextStyle(fontSize: 16, height: 1.5),
            ),
            if (item['takeaway'] != null) ...[
              const SizedBox(height: 12),
              Container(
                padding: const EdgeInsets.all(12),
                decoration: BoxDecoration(
                  color: Colors.white.withOpacity(0.7),
                  borderRadius: BorderRadius.circular(8),
                ),
                child: Row(
                  children: [
                    Icon(Icons.lightbulb_outline, size: 20, color: Colors.blue[700]),
                    const SizedBox(width: 8),
                    Expanded(
                      child: Text(
                        item['takeaway'],
                        style: TextStyle(
                          fontSize: 14,
                          fontStyle: FontStyle.italic,
                          color: Colors.blue[900],
                        ),
                      ),
                    ),
                  ],
                ),
              ),
            ],
          ],
        ),
      ),
    );
  }
}

