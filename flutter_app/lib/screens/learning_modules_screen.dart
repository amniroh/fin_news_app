import 'package:flutter/material.dart';
import '../services/api_service.dart';
import '../services/user_service.dart';

class LearningModulesScreen extends StatefulWidget {
  const LearningModulesScreen({super.key});

  @override
  State<LearningModulesScreen> createState() => _LearningModulesScreenState();
}

class _LearningModulesScreenState extends State<LearningModulesScreen> {
  List<Map<String, dynamic>> _modules = [];
  bool _isLoading = true;

  @override
  void initState() {
    super.initState();
    _loadModules();
  }

  Future<void> _loadModules() async {
    try {
      final response = await ApiService.getLearningModules();
      setState(() {
        _modules = List<Map<String, dynamic>>.from(response['modules'] ?? []);
        _isLoading = false;
      });
    } catch (e) {
      setState(() => _isLoading = false);
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text('Error loading modules: $e')),
        );
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: const Text('Learning Modules'),
        backgroundColor: Colors.blue[700],
        foregroundColor: Colors.white,
      ),
      body: _isLoading
          ? const Center(child: CircularProgressIndicator())
          : _modules.isEmpty
              ? const Center(child: Text('No modules available'))
              : ListView.builder(
                  padding: const EdgeInsets.all(16),
                  itemCount: _modules.length,
                  itemBuilder: (context, index) {
                    return _buildModuleCard(_modules[index]);
                  },
                ),
    );
  }

  Widget _buildModuleCard(Map<String, dynamic> module) {
    final id = module['id'] ?? '';
    final title = module['title'] ?? 'Module';
    final duration = module['duration'] ?? 60;
    final difficulty = module['difficulty'] ?? 'beginner';
    final description = module['description'] ?? '';

    Color difficultyColor;
    switch (difficulty) {
      case 'beginner':
        difficultyColor = Colors.green;
        break;
      case 'intermediate':
        difficultyColor = Colors.orange;
        break;
      case 'advanced':
        difficultyColor = Colors.red;
        break;
      default:
        difficultyColor = Colors.grey;
    }

    return Card(
      margin: const EdgeInsets.only(bottom: 16),
      child: InkWell(
        onTap: () => _openModule(id),
        child: Padding(
          padding: const EdgeInsets.all(16),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            mainAxisSize: MainAxisSize.min,
            children: [
              Row(
                children: [
                  Expanded(
                    child: Text(
                      title,
                      style: const TextStyle(
                        fontSize: 18,
                        fontWeight: FontWeight.bold,
                      ),
                    ),
                  ),
                  Container(
                    padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
                    decoration: BoxDecoration(
                      color: difficultyColor.withOpacity(0.2),
                      borderRadius: BorderRadius.circular(12),
                    ),
                    child: Text(
                      difficulty.toUpperCase(),
                      style: TextStyle(
                        color: difficultyColor,
                        fontSize: 11,
                        fontWeight: FontWeight.bold,
                      ),
                    ),
                  ),
                ],
              ),
              const SizedBox(height: 8),
              Text(
                description,
                style: TextStyle(color: Colors.grey[600], fontSize: 14),
                maxLines: 2,
                overflow: TextOverflow.ellipsis,
              ),
              const SizedBox(height: 12),
              Row(
                children: [
                  Icon(Icons.access_time, size: 16, color: Colors.grey[600]),
                  const SizedBox(width: 4),
                  Text(
                    '$duration seconds',
                    style: TextStyle(color: Colors.grey[600], fontSize: 14),
                  ),
                  const Spacer(),
                  Icon(Icons.arrow_forward_ios, size: 16, color: Colors.blue[700]),
                ],
              ),
            ],
          ),
        ),
      ),
    );
  }

  Future<void> _openModule(String moduleId) async {
    try {
      final userId = await UserService.getUserId();
      if (userId == null) {
        // Generate a user ID if doesn't exist
        final newUserId = 'user_${DateTime.now().millisecondsSinceEpoch}';
        await UserService.setUserId(newUserId);
        final response = await ApiService.getModuleContent(moduleId, newUserId);
        if (mounted) {
          Navigator.of(context).push(
            MaterialPageRoute(
              builder: (context) => ModuleContentScreen(moduleData: response),
            ),
          );
        }
        return;
      }

      final response = await ApiService.getModuleContent(moduleId, userId);
      
      if (mounted) {
        Navigator.of(context).push(
          MaterialPageRoute(
            builder: (context) => ModuleContentScreen(moduleData: response),
          ),
        );
      }
    } catch (e) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text('Error loading module: $e')),
        );
      }
    }
  }
}

class ModuleContentScreen extends StatelessWidget {
  final Map<String, dynamic> moduleData;

  const ModuleContentScreen({super.key, required this.moduleData});

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: Text(moduleData['title'] ?? 'Module'),
        backgroundColor: Colors.blue[700],
        foregroundColor: Colors.white,
      ),
      body: SingleChildScrollView(
        padding: const EdgeInsets.all(24),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              moduleData['content'] ?? '',
              style: const TextStyle(fontSize: 16, height: 1.6),
            ),
            if (moduleData['analogy'] != null) ...[
              const SizedBox(height: 24),
              Container(
                padding: const EdgeInsets.all(16),
                decoration: BoxDecoration(
                  color: Colors.blue[50],
                  borderRadius: BorderRadius.circular(12),
                ),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Row(
                      children: [
                        Icon(Icons.lightbulb, color: Colors.blue[700]),
                        const SizedBox(width: 8),
                        const Text(
                          'Simple Analogy',
                          style: TextStyle(
                            fontWeight: FontWeight.bold,
                            fontSize: 16,
                          ),
                        ),
                      ],
                    ),
                    const SizedBox(height: 8),
                    Text(moduleData['analogy']),
                  ],
                ),
              ),
            ],
            if (moduleData['quiz_question'] != null) ...[
              const SizedBox(height: 24),
              _buildQuiz(context),
            ],
          ],
        ),
      ),
    );
  }

  Widget _buildQuiz(BuildContext context) {
    final question = moduleData['quiz_question'] ?? '';
    final options = List<String>.from(moduleData['quiz_options'] ?? []);
    final correctAnswer = moduleData['correct_answer'] ?? 0;

    return Container(
      padding: const EdgeInsets.all(16),
      decoration: BoxDecoration(
        color: Colors.purple[50],
        borderRadius: BorderRadius.circular(12),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          const Text(
            'Quick Quiz',
            style: TextStyle(
              fontWeight: FontWeight.bold,
              fontSize: 18,
            ),
          ),
          const SizedBox(height: 12),
          Text(question),
          const SizedBox(height: 16),
          ...options.asMap().entries.map((entry) {
            final index = entry.key;
            final option = entry.value;
            return Padding(
              padding: const EdgeInsets.only(bottom: 8),
              child: ElevatedButton(
                onPressed: () {
                  final isCorrect = index == correctAnswer;
                  ScaffoldMessenger.of(context).showSnackBar(
                    SnackBar(
                      content: Text(isCorrect ? 'Correct! 🎉' : 'Not quite. Try again!'),
                      backgroundColor: isCorrect ? Colors.green : Colors.orange,
                    ),
                  );
                  if (isCorrect) {
                    Navigator.of(context).pop();
                  }
                },
                style: ElevatedButton.styleFrom(
                  backgroundColor: Colors.white,
                  foregroundColor: Colors.black87,
                  minimumSize: const Size(double.infinity, 50),
                ),
                child: Text(option),
              ),
            );
          }),
        ],
      ),
    );
  }
}

